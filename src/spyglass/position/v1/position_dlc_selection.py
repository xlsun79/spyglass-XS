from pathlib import Path

import datajoint as dj
import numpy as np
import pandas as pd
import pynwb
from tqdm import tqdm as tqdm

from ...common.common_nwbfile import AnalysisNwbfile
from ...utils.dj_helper_fn import fetch_nwb
from .dlc_utils import get_video_path, make_video
from .position_dlc_centroid import DLCCentroid
from .position_dlc_cohort import DLCSmoothInterpCohort
from .position_dlc_orient import DLCOrientation
from .position_dlc_pose_estimation import DLCPoseEstimation, DLCPoseEstimationSelection
from .position_dlc_position import DLCSmoothInterpParams

schema = dj.schema("position_v1_dlc_selection")


@schema
class DLCPosSelection(dj.Manual):
    """
    Specify collection of upstream DLCCentroid and DLCOrientation entries
    to combine into a set of position information
    """

    definition = """
    -> DLCCentroid.proj(dlc_si_cohort_centroid='dlc_si_cohort_selection_name', centroid_analysis_file_name='analysis_file_name')
    -> DLCOrientation.proj(dlc_si_cohort_orientation='dlc_si_cohort_selection_name', orientation_analysis_file_name='analysis_file_name')
    """


@schema
class DLCPosV1(dj.Computed):
    """
    Combines upstream DLCCentroid and DLCOrientation
    entries into a single entry with a single Analysis NWB file
    """

    definition = """
    -> DLCPosSelection
    ---
    -> AnalysisNwbfile
    position_object_id      : varchar(80)
    orientation_object_id   : varchar(80)
    velocity_object_id      : varchar(80)
    pose_eval_result        : longblob
    """

    def make(self, key):
        key["pose_eval_result"] = self.evaluate_pose_estimation(key)
        position_nwb_data = (DLCCentroid & key).fetch_nwb()[0]
        orientation_nwb_data = (DLCOrientation & key).fetch_nwb()[0]
        position_object = position_nwb_data["dlc_position"].spatial_series["position"]
        velocity_object = position_nwb_data["dlc_velocity"].time_series["velocity"]
        video_frame_object = position_nwb_data["dlc_velocity"].time_series[
            "video_frame_ind"
        ]
        orientation_object = orientation_nwb_data["dlc_orientation"].spatial_series[
            "orientation"
        ]
        position = pynwb.behavior.Position()
        orientation = pynwb.behavior.CompassDirection()
        velocity = pynwb.behavior.BehavioralTimeSeries()
        position.create_spatial_series(
            name=position_object.name,
            timestamps=np.asarray(position_object.timestamps),
            conversion=position_object.conversion,
            data=np.asarray(position_object.data),
            reference_frame=position_object.reference_frame,
            comments=position_object.comments,
            description=position_object.description,
        )
        orientation.create_spatial_series(
            name=orientation_object.name,
            timestamps=np.asarray(orientation_object.timestamps),
            conversion=orientation_object.conversion,
            data=np.asarray(orientation_object.data),
            reference_frame=orientation_object.reference_frame,
            comments=orientation_object.comments,
            description=orientation_object.description,
        )
        velocity.create_timeseries(
            name=velocity_object.name,
            timestamps=np.asarray(velocity_object.timestamps),
            conversion=velocity_object.conversion,
            unit=velocity_object.unit,
            data=np.asarray(velocity_object.data),
            comments=velocity_object.comments,
            description=velocity_object.description,
        )
        velocity.create_timeseries(
            name=video_frame_object.name,
            unit=video_frame_object.unit,
            timestamps=np.asarray(video_frame_object.timestamps),
            data=np.asarray(video_frame_object.data),
            description=video_frame_object.description,
            comments=video_frame_object.comments,
        )
        # Add to Analysis NWB file
        key["analysis_file_name"] = AnalysisNwbfile().create(key["nwb_file_name"])
        nwb_analysis_file = AnalysisNwbfile()
        key["orientation_object_id"] = nwb_analysis_file.add_nwb_object(
            key["analysis_file_name"], orientation
        )
        key["position_object_id"] = nwb_analysis_file.add_nwb_object(
            key["analysis_file_name"], position
        )
        key["velocity_object_id"] = nwb_analysis_file.add_nwb_object(
            key["analysis_file_name"], velocity
        )

        nwb_analysis_file.add(
            nwb_file_name=key["nwb_file_name"],
            analysis_file_name=key["analysis_file_name"],
        )

        self.insert1(key)
        from ..position_merge import PositionOutput

        key["source"] = "DLC"
        key["version"] = 1
        dlc_key = key.copy()
        del dlc_key["pose_eval_result"]
        key["interval_list_name"] = f"pos {key['epoch']-1} valid times"
        valid_fields = PositionOutput().fetch().dtype.fields.keys()
        entries_to_delete = [entry for entry in key.keys() if entry not in valid_fields]
        for entry in entries_to_delete:
            del key[entry]

        PositionOutput().insert1(key=key, params=dlc_key, skip_duplicates=True)

    def fetch_nwb(self, *attrs, **kwargs):
        return fetch_nwb(
            self, (AnalysisNwbfile, "analysis_file_abs_path"), *attrs, **kwargs
        )

    def fetch1_dataframe(self):
        nwb_data = self.fetch_nwb()[0]
        index = pd.Index(
            np.asarray(nwb_data["position"].get_spatial_series().timestamps),
            name="time",
        )
        COLUMNS = [
            "video_frame_ind",
            "position_x",
            "position_y",
            "orientation",
            "velocity_x",
            "velocity_y",
            "speed",
        ]
        return pd.DataFrame(
            np.concatenate(
                (
                    np.asarray(
                        nwb_data["velocity"].time_series["video_frame_ind"].data,
                        dtype=int,
                    )[:, np.newaxis],
                    np.asarray(nwb_data["position"].get_spatial_series().data),
                    np.asarray(nwb_data["orientation"].get_spatial_series().data)[
                        :, np.newaxis
                    ],
                    np.asarray(nwb_data["velocity"].time_series["velocity"].data),
                ),
                axis=1,
            ),
            columns=COLUMNS,
            index=index,
        )

    @classmethod
    def evaluate_pose_estimation(cls, key):
        likelihood_thresh = []
        valid_fields = DLCSmoothInterpCohort.BodyPart().fetch().dtype.fields.keys()
        centroid_key = {k: val for k, val in key.items() if k in valid_fields}
        centroid_key["dlc_si_cohort_selection_name"] = key["dlc_si_cohort_centroid"]
        orientation_key = centroid_key.copy()
        orientation_key["dlc_si_cohort_selection_name"] = key[
            "dlc_si_cohort_orientation"
        ]
        centroid_bodyparts, centroid_si_params = (
            DLCSmoothInterpCohort.BodyPart & centroid_key
        ).fetch("bodypart", "dlc_si_params_name")
        orientation_bodyparts, orientation_si_params = (
            DLCSmoothInterpCohort.BodyPart & orientation_key
        ).fetch("bodypart", "dlc_si_params_name")
        for param in np.unique(
            np.concatenate((centroid_si_params, orientation_si_params))
        ):
            likelihood_thresh.append(
                (DLCSmoothInterpParams() & {"dlc_si_params_name": param}).fetch1(
                    "params"
                )["likelihood_thresh"]
            )

        if len(np.unique(likelihood_thresh)) > 1:
            raise ValueError("more than one likelihood threshold used")
        like_thresh = likelihood_thresh[0]
        bodyparts = np.unique([*centroid_bodyparts, *orientation_bodyparts])
        fields = list(DLCPoseEstimation.BodyPart.fetch().dtype.fields.keys())
        pose_estimation_key = {k: v for k, v in key.items() if k in fields}
        pose_estimation_df = pd.concat(
            {
                bodypart: (
                    DLCPoseEstimation.BodyPart()
                    & {**pose_estimation_key, **{"bodypart": bodypart}}
                ).fetch1_dataframe()
                for bodypart in bodyparts.tolist()
            },
            axis=1,
        )
        df_filter = {
            bodypart: pose_estimation_df[bodypart]["likelihood"] < like_thresh
            for bodypart in bodyparts
            if bodypart in pose_estimation_df.columns
        }
        sub_thresh_ind_dict = {
            bodypart: {
                "inds": np.where(
                    ~np.isnan(
                        pose_estimation_df[bodypart]["likelihood"].where(
                            df_filter[bodypart]
                        )
                    )
                )[0],
            }
            for bodypart in bodyparts
        }
        sub_thresh_percent_dict = {
            bodypart: (
                len(
                    np.where(
                        ~np.isnan(
                            pose_estimation_df[bodypart]["likelihood"].where(
                                df_filter[bodypart]
                            )
                        )
                    )[0]
                )
                / len(pose_estimation_df)
            )
            * 100
            for bodypart in bodyparts
        }
        return sub_thresh_percent_dict


@schema
class DLCPosVideoParams(dj.Manual):
    definition = """
    dlc_pos_video_params_name : varchar(50)
    ---
    params : blob
    """

    @classmethod
    def insert_default(cls):
        params = {
            "percent_frames": 1,
            "incl_likelihood": True,
            "video_params": {
                "arrow_radius": 20,
                "circle_radius": 6,
            },
        }
        cls.insert1(
            {"dlc_pos_video_params_name": "default", "params": params},
            skip_duplicates=True,
        )

    @classmethod
    def get_default(cls):
        query = cls & {"dlc_pos_video_params_name": "default"}
        if not len(query) > 0:
            cls().insert_default(skip_duplicates=True)
            default = (cls & {"dlc_pos_video_params_name": "default"}).fetch1()
        else:
            default = query.fetch1()
        return default


@schema
class DLCPosVideoSelection(dj.Manual):
    definition = """
    -> DLCPosV1
    -> DLCPosVideoParams
    ---
    """


@schema
class DLCPosVideo(dj.Computed):
    """Creates a video of the computed head position and orientation as well as
    the original LED positions overlaid on the video of the animal.

    Use for debugging the effect of position extraction parameters."""

    definition = """
    -> DLCPosVideoSelection
    ---
    """

    def make(self, key):
        from tqdm import tqdm as tqdm

        params = (DLCPosVideoParams & key).fetch1("params")
        M_TO_CM = 100
        key["interval_list_name"] = f"pos {key['epoch']-1} valid times"
        epoch = (
            int(
                key["interval_list_name"]
                .replace("pos ", "")
                .replace(" valid times", "")
            )
            + 1
        )
        pose_estimation_key = {
            "nwb_file_name": key["nwb_file_name"],
            "epoch": epoch,
            "dlc_model_name": key["dlc_model_name"],
            "dlc_model_params_name": key["dlc_model_params_name"],
        }
        pose_estimation_params, video_filename, output_dir = (
            DLCPoseEstimationSelection() & pose_estimation_key
        ).fetch1("pose_estimation_params", "video_path", "pose_estimation_output_dir")
        print(f"video filename: {video_filename}")
        meters_per_pixel = (DLCPoseEstimation() & pose_estimation_key).fetch1(
            "meters_per_pixel"
        )
        crop = None
        if "cropping" in pose_estimation_params:
            crop = pose_estimation_params["cropping"]
        print("Loading position data...")
        position_info_df = (
            DLCPosV1()
            & {
                "nwb_file_name": key["nwb_file_name"],
                "epoch": epoch,
                "dlc_si_cohort_centroid": key["dlc_si_cohort_centroid"],
                "dlc_centroid_params_name": key["dlc_centroid_params_name"],
                "dlc_si_cohort_orientation": key["dlc_si_cohort_orientation"],
                "dlc_orientation_params_name": key["dlc_orientation_params_name"],
            }
        ).fetch1_dataframe()
        pose_estimation_df = pd.concat(
            {
                bodypart: (
                    DLCPoseEstimation.BodyPart()
                    & {**pose_estimation_key, **{"bodypart": bodypart}}
                ).fetch1_dataframe()
                for bodypart in (DLCSmoothInterpCohort.BodyPart & pose_estimation_key)
                .fetch("bodypart")
                .tolist()
            },
            axis=1,
        )
        assert len(pose_estimation_df) == len(position_info_df), (
            f"length of pose_estimation_df: {len(pose_estimation_df)} "
            f"does not match the length of position_info_df: {len(position_info_df)}."
        )

        nwb_base_filename = key["nwb_file_name"].replace(".nwb", "")
        if Path(output_dir).exists():
            output_video_filename = (
                f"{Path(output_dir).as_posix()}/"
                f"{nwb_base_filename}_{epoch:02d}_"
                f'{key["dlc_si_cohort_centroid"]}_'
                f'{key["dlc_centroid_params_name"]}'
                f'{key["dlc_orientation_params_name"]}.mp4'
            )
        else:
            output_video_filename = (
                f"{nwb_base_filename}_{epoch:02d}_"
                f'{key["dlc_si_cohort_centroid"]}_'
                f'{key["dlc_centroid_params_name"]}'
                f'{key["dlc_orientation_params_name"]}.mp4'
            )
        idx = pd.IndexSlice
        video_frame_inds = position_info_df["video_frame_ind"].astype(int).to_numpy()
        centroids = {
            bodypart: pose_estimation_df.loc[:, idx[bodypart, ("x", "y")]].to_numpy()
            for bodypart in pose_estimation_df.columns.levels[0]
        }
        if params.get("incl_likelihood", None):
            likelihoods = {
                bodypart: pose_estimation_df.loc[
                    :, idx[bodypart, ("likelihood")]
                ].to_numpy()
                for bodypart in pose_estimation_df.columns.levels[0]
            }
        else:
            likelihoods = None
        position_mean = {
            "DLC": np.asarray(position_info_df[["position_x", "position_y"]])
        }
        orientation_mean = {"DLC": np.asarray(position_info_df[["orientation"]])}
        position_time = np.asarray(position_info_df.index)
        cm_per_pixel = meters_per_pixel * M_TO_CM
        percent_frames = params.get("percent_frames", None)
        frames = params.get("frames", None)
        if frames is not None:
            frames_arr = np.arange(frames[0], frames[1])
        else:
            frames_arr = frames

        print("Making video...")
        make_video(
            video_filename=video_filename,
            video_frame_inds=video_frame_inds,
            position_mean=position_mean,
            orientation_mean=orientation_mean,
            centroids=centroids,
            likelihoods=likelihoods,
            position_time=position_time,
            video_time=None,
            processor=params.get("processor", "matplotlib"),
            frames=frames_arr,
            percent_frames=percent_frames,
            output_video_filename=output_video_filename,
            cm_to_pixels=cm_per_pixel,
            disable_progressbar=False,
            crop=crop,
            **params["video_params"],
        )
