import argparse
import datetime
import logging
import multiprocessing as mp
import os
import secrets
import shutil
import signal
import sys
import traceback
from multiprocessing import Queue
from multiprocessing.synchronize import Event as MpEvent
from types import FrameType
from typing import Optional

import psutil
from peewee_migrate import Router
from playhouse.sqlite_ext import SqliteExtDatabase
from playhouse.sqliteq import SqliteQueueDatabase
from pydantic import ValidationError

from frigate.api.app import create_app
from frigate.api.auth import hash_password
from frigate.comms.config_updater import ConfigPublisher
from frigate.comms.dispatcher import Communicator, Dispatcher
from frigate.comms.inter_process import InterProcessCommunicator
from frigate.comms.mqtt import MqttClient
from frigate.comms.webpush import WebPushClient
from frigate.comms.ws import WebSocketClient
from frigate.comms.zmq_proxy import ZmqProxy
from frigate.config import FrigateConfig
from frigate.const import (
    CACHE_DIR,
    CLIPS_DIR,
    CONFIG_DIR,
    DEFAULT_DB_PATH,
    EXPORT_DIR,
    MODEL_CACHE_DIR,
    RECORD_DIR,
)
from frigate.embeddings import EmbeddingsContext, manage_embeddings
from frigate.events.audio import listen_to_audio
from frigate.events.cleanup import EventCleanup
from frigate.events.external import ExternalEventProcessor
from frigate.events.maintainer import EventProcessor
from frigate.log import log_process, root_configurer
from frigate.models import (
    Event,
    Export,
    Previews,
    Recordings,
    RecordingsToDelete,
    Regions,
    ReviewSegment,
    Timeline,
    User,
)
from frigate.object_detection import ObjectDetectProcess
from frigate.object_processing import TrackedObjectProcessor
from frigate.output.output import output_frames
from frigate.plus import PlusApi
from frigate.ptz.autotrack import PtzAutoTrackerThread
from frigate.ptz.onvif import OnvifController
from frigate.record.cleanup import RecordingCleanup
from frigate.record.export import migrate_exports
from frigate.record.record import manage_recordings
from frigate.review.review import manage_review_segments
from frigate.stats.emitter import StatsEmitter
from frigate.stats.util import stats_init
from frigate.storage import StorageMaintainer
from frigate.timeline import TimelineProcessor
from frigate.types import CameraMetricsTypes, PTZMetricsTypes
from frigate.util.builtin import empty_and_close_queue, save_default_config
from frigate.util.config import migrate_frigate_config
from frigate.util.object import get_camera_regions_grid
from frigate.version import VERSION
from frigate.video import capture_camera, track_camera
from frigate.watchdog import FrigateWatchdog

logger = logging.getLogger(__name__)


class FrigateApp:
    def __init__(self) -> None:
        self.stop_event: MpEvent = mp.Event()
        self.detection_queue: Queue = mp.Queue()
        self.detectors: dict[str, ObjectDetectProcess] = {}
        self.detection_out_events: dict[str, MpEvent] = {}
        self.detection_shms: list[mp.shared_memory.SharedMemory] = []
        self.log_queue: Queue = mp.Queue()
        self.plus_api = PlusApi()
        self.camera_metrics: dict[str, CameraMetricsTypes] = {}
        self.ptz_metrics: dict[str, PTZMetricsTypes] = {}
        self.processes: dict[str, int] = {}
        self.region_grids: dict[str, list[list[dict[str, int]]]] = {}

    def set_environment_vars(self) -> None:
        for key, value in self.config.environment_vars.items():
            os.environ[key] = value

    def ensure_dirs(self) -> None:
        for d in [
            CONFIG_DIR,
            RECORD_DIR,
            f"{CLIPS_DIR}/cache",
            CACHE_DIR,
            MODEL_CACHE_DIR,
            EXPORT_DIR,
        ]:
            if not os.path.exists(d) and not os.path.islink(d):
                logger.info(f"Creating directory: {d}")
                os.makedirs(d)
            else:
                logger.debug(f"Skipping directory: {d}")

    def init_logger(self) -> None:
        self.log_process = mp.Process(
            target=log_process, args=(self.log_queue,), name="log_process"
        )
        self.log_process.daemon = True
        self.log_process.start()
        self.processes["logger"] = self.log_process.pid or 0
        root_configurer(self.log_queue)

    def init_config(self) -> None:
        config_file = os.environ.get("CONFIG_FILE", "/config/config.yml")

        # Check if we can use .yaml instead of .yml
        config_file_yaml = config_file.replace(".yml", ".yaml")
        if os.path.isfile(config_file_yaml):
            config_file = config_file_yaml

        if not os.path.isfile(config_file):
            print("No config file found, saving default config")
            config_file = config_file_yaml
            save_default_config(config_file)

        # check if the config file needs to be migrated
        migrate_frigate_config(config_file)

        user_config = FrigateConfig.parse_file(config_file)
        self.config = user_config.runtime_config(self.plus_api)

        for camera_name in self.config.cameras.keys():
            # create camera_metrics
            self.camera_metrics[camera_name] = {
                "camera_fps": mp.Value("d", 0.0),  # type: ignore[typeddict-item]
                # issue https://github.com/python/typeshed/issues/8799
                # from mypy 0.981 onwards
                "skipped_fps": mp.Value("d", 0.0),  # type: ignore[typeddict-item]
                # issue https://github.com/python/typeshed/issues/8799
                # from mypy 0.981 onwards
                "process_fps": mp.Value("d", 0.0),  # type: ignore[typeddict-item]
                "detection_fps": mp.Value("d", 0.0),  # type: ignore[typeddict-item]
                # issue https://github.com/python/typeshed/issues/8799
                # from mypy 0.981 onwards
                "detection_frame": mp.Value("d", 0.0),  # type: ignore[typeddict-item]
                # issue https://github.com/python/typeshed/issues/8799
                # from mypy 0.981 onwards
                "read_start": mp.Value("d", 0.0),  # type: ignore[typeddict-item]
                # issue https://github.com/python/typeshed/issues/8799
                # from mypy 0.981 onwards
                "ffmpeg_pid": mp.Value("i", 0),  # type: ignore[typeddict-item]
                # issue https://github.com/python/typeshed/issues/8799
                # from mypy 0.981 onwards
                "frame_queue": mp.Queue(maxsize=2),
                "capture_process": None,
                "process": None,
                "audio_rms": mp.Value("d", 0.0),  # type: ignore[typeddict-item]
                "audio_dBFS": mp.Value("d", 0.0),  # type: ignore[typeddict-item]
            }
            self.ptz_metrics[camera_name] = {
                "ptz_autotracker_enabled": mp.Value(  # type: ignore[typeddict-item]
                    # issue https://github.com/python/typeshed/issues/8799
                    # from mypy 0.981 onwards
                    "i",
                    self.config.cameras[camera_name].onvif.autotracking.enabled,
                ),
                "ptz_tracking_active": mp.Event(),
                "ptz_motor_stopped": mp.Event(),
                "ptz_reset": mp.Event(),
                "ptz_start_time": mp.Value("d", 0.0),  # type: ignore[typeddict-item]
                # issue https://github.com/python/typeshed/issues/8799
                # from mypy 0.981 onwards
                "ptz_stop_time": mp.Value("d", 0.0),  # type: ignore[typeddict-item]
                # issue https://github.com/python/typeshed/issues/8799
                # from mypy 0.981 onwards
                "ptz_frame_time": mp.Value("d", 0.0),  # type: ignore[typeddict-item]
                # issue https://github.com/python/typeshed/issues/8799
                # from mypy 0.981 onwards
                "ptz_zoom_level": mp.Value("d", 0.0),  # type: ignore[typeddict-item]
                # issue https://github.com/python/typeshed/issues/8799
                # from mypy 0.981 onwards
                "ptz_max_zoom": mp.Value("d", 0.0),  # type: ignore[typeddict-item]
                # issue https://github.com/python/typeshed/issues/8799
                # from mypy 0.981 onwards
                "ptz_min_zoom": mp.Value("d", 0.0),  # type: ignore[typeddict-item]
                # issue https://github.com/python/typeshed/issues/8799
                # from mypy 0.981 onwards
            }
            self.ptz_metrics[camera_name]["ptz_motor_stopped"].set()

    def set_log_levels(self) -> None:
        logging.getLogger().setLevel(self.config.logger.default.value.upper())
        for log, level in self.config.logger.logs.items():
            logging.getLogger(log).setLevel(level.value.upper())

        if "werkzeug" not in self.config.logger.logs:
            logging.getLogger("werkzeug").setLevel("ERROR")

        if "ws4py" not in self.config.logger.logs:
            logging.getLogger("ws4py").setLevel("ERROR")

    def init_queues(self) -> None:
        # Queue for cameras to push tracked objects to
        self.detected_frames_queue: Queue = mp.Queue(
            maxsize=sum(camera.enabled for camera in self.config.cameras.values()) * 2
        )

        # Queue for timeline events
        self.timeline_queue: Queue = mp.Queue()

    def init_database(self) -> None:
        def vacuum_db(db: SqliteExtDatabase) -> None:
            logger.info("Running database vacuum")
            db.execute_sql("VACUUM;")

            try:
                with open(f"{CONFIG_DIR}/.vacuum", "w") as f:
                    f.write(str(datetime.datetime.now().timestamp()))
            except PermissionError:
                logger.error("Unable to write to /config to save DB state")

        def cleanup_timeline_db(db: SqliteExtDatabase) -> None:
            db.execute_sql(
                "DELETE FROM timeline WHERE source_id NOT IN (SELECT id FROM event);"
            )

            try:
                with open(f"{CONFIG_DIR}/.timeline", "w") as f:
                    f.write(str(datetime.datetime.now().timestamp()))
            except PermissionError:
                logger.error("Unable to write to /config to save DB state")

        # Migrate DB location
        old_db_path = DEFAULT_DB_PATH
        if not os.path.isfile(self.config.database.path) and os.path.isfile(
            old_db_path
        ):
            os.rename(old_db_path, self.config.database.path)

        # Migrate DB schema
        migrate_db = SqliteExtDatabase(self.config.database.path)

        # Run migrations
        del logging.getLogger("peewee_migrate").handlers[:]
        router = Router(migrate_db)

        if len(router.diff) > 0:
            logger.info("Making backup of DB before migrations...")
            shutil.copyfile(
                self.config.database.path,
                self.config.database.path.replace("frigate.db", "backup.db"),
            )

        router.run()

        # this is a temporary check to clean up user DB from beta
        # will be removed before final release
        if not os.path.exists(f"{CONFIG_DIR}/.timeline"):
            cleanup_timeline_db(migrate_db)

        # check if vacuum needs to be run
        if os.path.exists(f"{CONFIG_DIR}/.vacuum"):
            with open(f"{CONFIG_DIR}/.vacuum") as f:
                try:
                    timestamp = round(float(f.readline()))
                except Exception:
                    timestamp = 0

                if (
                    timestamp
                    < (
                        datetime.datetime.now() - datetime.timedelta(weeks=2)
                    ).timestamp()
                ):
                    vacuum_db(migrate_db)
        else:
            vacuum_db(migrate_db)

        migrate_db.close()

    def init_go2rtc(self) -> None:
        for proc in psutil.process_iter(["pid", "name"]):
            if proc.info["name"] == "go2rtc":
                logger.info(f"go2rtc process pid: {proc.info['pid']}")
                self.processes["go2rtc"] = proc.info["pid"]

    def init_recording_manager(self) -> None:
        recording_process = mp.Process(
            target=manage_recordings,
            name="recording_manager",
            args=(self.config,),
        )
        recording_process.daemon = True
        self.recording_process = recording_process
        recording_process.start()
        self.processes["recording"] = recording_process.pid or 0
        logger.info(f"Recording process started: {recording_process.pid}")

    def init_review_segment_manager(self) -> None:
        review_segment_process = mp.Process(
            target=manage_review_segments,
            name="review_segment_manager",
            args=(self.config,),
        )
        review_segment_process.daemon = True
        self.review_segment_process = review_segment_process
        review_segment_process.start()
        self.processes["review_segment"] = review_segment_process.pid or 0
        logger.info(f"Review process started: {review_segment_process.pid}")

    def init_embeddings_manager(self) -> None:
        if not self.config.semantic_search.enabled:
            self.embeddings = None
            return

        # Create a client for other processes to use
        self.embeddings = EmbeddingsContext()
        embedding_process = mp.Process(
            target=manage_embeddings,
            name="embeddings_manager",
            args=(self.config,),
        )
        embedding_process.daemon = True
        self.embedding_process = embedding_process
        embedding_process.start()
        self.processes["embeddings"] = embedding_process.pid or 0
        logger.info(f"Embedding process started: {embedding_process.pid}")

    def bind_database(self) -> None:
        """Bind db to the main process."""
        # NOTE: all db accessing processes need to be created before the db can be bound to the main process
        self.db = SqliteQueueDatabase(
            self.config.database.path,
            pragmas={
                "auto_vacuum": "FULL",  # Does not defragment database
                "cache_size": -512 * 1000,  # 512MB of cache,
                "synchronous": "NORMAL",  # Safe when using WAL https://www.sqlite.org/pragma.html#pragma_synchronous
            },
            timeout=max(
                60, 10 * len([c for c in self.config.cameras.values() if c.enabled])
            ),
        )
        models = [
            Event,
            Export,
            Previews,
            Recordings,
            RecordingsToDelete,
            Regions,
            ReviewSegment,
            Timeline,
            User,
        ]
        self.db.bind(models)

    def check_db_data_migrations(self) -> None:
        # check if vacuum needs to be run
        if not os.path.exists(f"{CONFIG_DIR}/.exports"):
            try:
                with open(f"{CONFIG_DIR}/.exports", "w") as f:
                    f.write(str(datetime.datetime.now().timestamp()))
            except PermissionError:
                logger.error("Unable to write to /config to save export state")

            migrate_exports(self.config.ffmpeg, self.config.cameras.keys())

    def init_external_event_processor(self) -> None:
        self.external_event_processor = ExternalEventProcessor(self.config)

    def init_inter_process_communicator(self) -> None:
        self.inter_process_communicator = InterProcessCommunicator()
        self.inter_config_updater = ConfigPublisher()
        self.inter_zmq_proxy = ZmqProxy()

    def init_web_server(self) -> None:
        self.flask_app = create_app(
            self.config,
            self.db,
            self.embeddings,
            self.detected_frames_processor,
            self.storage_maintainer,
            self.onvif_controller,
            self.external_event_processor,
            self.plus_api,
            self.stats_emitter,
        )

    def init_onvif(self) -> None:
        self.onvif_controller = OnvifController(self.config, self.ptz_metrics)

    def init_dispatcher(self) -> None:
        comms: list[Communicator] = []

        if self.config.mqtt.enabled:
            comms.append(MqttClient(self.config))

        if self.config.notifications.enabled_in_config:
            comms.append(WebPushClient(self.config))

        comms.append(WebSocketClient(self.config))
        comms.append(self.inter_process_communicator)

        self.dispatcher = Dispatcher(
            self.config,
            self.inter_config_updater,
            self.onvif_controller,
            self.ptz_metrics,
            comms,
        )

    def start_detectors(self) -> None:
        for name in self.config.cameras.keys():
            self.detection_out_events[name] = mp.Event()

            try:
                largest_frame = max(
                    [
                        det.model.height * det.model.width * 3
                        for (name, det) in self.config.detectors.items()
                    ]
                )
                shm_in = mp.shared_memory.SharedMemory(
                    name=name,
                    create=True,
                    size=largest_frame,
                )
            except FileExistsError:
                shm_in = mp.shared_memory.SharedMemory(name=name)

            try:
                shm_out = mp.shared_memory.SharedMemory(
                    name=f"out-{name}", create=True, size=20 * 6 * 4
                )
            except FileExistsError:
                shm_out = mp.shared_memory.SharedMemory(name=f"out-{name}")

            self.detection_shms.append(shm_in)
            self.detection_shms.append(shm_out)

        for name, detector_config in self.config.detectors.items():
            self.detectors[name] = ObjectDetectProcess(
                name,
                self.detection_queue,
                self.detection_out_events,
                detector_config,
            )

    def start_ptz_autotracker(self) -> None:
        self.ptz_autotracker_thread = PtzAutoTrackerThread(
            self.config,
            self.onvif_controller,
            self.ptz_metrics,
            self.dispatcher,
            self.stop_event,
        )
        self.ptz_autotracker_thread.start()

    def start_detected_frames_processor(self) -> None:
        self.detected_frames_processor = TrackedObjectProcessor(
            self.config,
            self.dispatcher,
            self.detected_frames_queue,
            self.ptz_autotracker_thread,
            self.stop_event,
        )
        self.detected_frames_processor.start()

    def start_video_output_processor(self) -> None:
        output_processor = mp.Process(
            target=output_frames,
            name="output_processor",
            args=(self.config,),
        )
        output_processor.daemon = True
        self.output_processor = output_processor
        output_processor.start()
        logger.info(f"Output process started: {output_processor.pid}")

    def init_historical_regions(self) -> None:
        # delete region grids for removed or renamed cameras
        cameras = list(self.config.cameras.keys())
        Regions.delete().where(~(Regions.camera << cameras)).execute()

        # create or update region grids for each camera
        for camera in self.config.cameras.values():
            self.region_grids[camera.name] = get_camera_regions_grid(
                camera.name,
                camera.detect,
                max(self.config.model.width, self.config.model.height),
            )

    def start_camera_processors(self) -> None:
        for name, config in self.config.cameras.items():
            if not self.config.cameras[name].enabled:
                logger.info(f"Camera processor not started for disabled camera {name}")
                continue

            camera_process = mp.Process(
                target=track_camera,
                name=f"camera_processor:{name}",
                args=(
                    name,
                    config,
                    self.config.model,
                    self.config.model.merged_labelmap,
                    self.detection_queue,
                    self.detection_out_events[name],
                    self.detected_frames_queue,
                    self.camera_metrics[name],
                    self.ptz_metrics[name],
                    self.region_grids[name],
                ),
            )
            camera_process.daemon = True
            self.camera_metrics[name]["process"] = camera_process
            camera_process.start()
            logger.info(f"Camera processor started for {name}: {camera_process.pid}")

    def start_camera_capture_processes(self) -> None:
        for name, config in self.config.cameras.items():
            if not self.config.cameras[name].enabled:
                logger.info(f"Capture process not started for disabled camera {name}")
                continue

            capture_process = mp.Process(
                target=capture_camera,
                name=f"camera_capture:{name}",
                args=(name, config, self.shm_frame_count, self.camera_metrics[name]),
            )
            capture_process.daemon = True
            self.camera_metrics[name]["capture_process"] = capture_process
            capture_process.start()
            logger.info(f"Capture process started for {name}: {capture_process.pid}")

    def start_audio_processors(self) -> None:
        self.audio_process = None
        if len([c for c in self.config.cameras.values() if c.audio.enabled]) > 0:
            self.audio_process = mp.Process(
                target=listen_to_audio,
                name="audio_capture",
                args=(
                    self.config,
                    self.camera_metrics,
                ),
            )
            self.audio_process.daemon = True
            self.audio_process.start()
            self.processes["audio_detector"] = self.audio_process.pid or 0
            logger.info(f"Audio process started: {self.audio_process.pid}")

    def start_timeline_processor(self) -> None:
        self.timeline_processor = TimelineProcessor(
            self.config, self.timeline_queue, self.stop_event
        )
        self.timeline_processor.start()

    def start_event_processor(self) -> None:
        self.event_processor = EventProcessor(
            self.config,
            self.timeline_queue,
            self.stop_event,
        )
        self.event_processor.start()

    def start_event_cleanup(self) -> None:
        self.event_cleanup = EventCleanup(self.config, self.stop_event)
        self.event_cleanup.start()

    def start_record_cleanup(self) -> None:
        self.record_cleanup = RecordingCleanup(self.config, self.stop_event)
        self.record_cleanup.start()

    def start_storage_maintainer(self) -> None:
        self.storage_maintainer = StorageMaintainer(self.config, self.stop_event)
        self.storage_maintainer.start()

    def start_stats_emitter(self) -> None:
        self.stats_emitter = StatsEmitter(
            self.config,
            stats_init(
                self.config, self.camera_metrics, self.detectors, self.processes
            ),
            self.stop_event,
        )
        self.stats_emitter.start()

    def start_watchdog(self) -> None:
        self.frigate_watchdog = FrigateWatchdog(self.detectors, self.stop_event)
        self.frigate_watchdog.start()

    def check_shm(self) -> None:
        total_shm = round(shutil.disk_usage("/dev/shm").total / pow(2, 20), 1)

        # required for log files + nginx cache
        min_req_shm = 40 + 10

        if self.config.birdseye.restream:
            min_req_shm += 8

        available_shm = total_shm - min_req_shm
        cam_total_frame_size = 0

        for camera in self.config.cameras.values():
            if camera.enabled:
                cam_total_frame_size += round(
                    (camera.detect.width * camera.detect.height * 1.5 + 270480)
                    / 1048576,
                    1,
                )

        self.shm_frame_count = min(50, int(available_shm / (cam_total_frame_size)))

        logger.debug(
            f"Calculated total camera size {available_shm} / {cam_total_frame_size} :: {self.shm_frame_count} frames for each camera in SHM"
        )

        if self.shm_frame_count < 10:
            logger.warning(
                f"The current SHM size of {total_shm}MB is too small, recommend increasing it to at least {round(min_req_shm + cam_total_frame_size)}MB."
            )

    def init_auth(self) -> None:
        if self.config.auth.enabled:
            if User.select().count() == 0:
                password = secrets.token_hex(16)
                password_hash = hash_password(
                    password, iterations=self.config.auth.hash_iterations
                )
                User.insert(
                    {
                        User.username: "admin",
                        User.password_hash: password_hash,
                    }
                ).execute()

                logger.info("********************************************************")
                logger.info("********************************************************")
                logger.info("***    Auth is enabled, but no users exist.          ***")
                logger.info("***    Created a default user:                       ***")
                logger.info("***    User: admin                                   ***")
                logger.info(f"***    Password: {password}   ***")
                logger.info("********************************************************")
                logger.info("********************************************************")
            elif self.config.auth.reset_admin_password:
                password = secrets.token_hex(16)
                password_hash = hash_password(
                    password, iterations=self.config.auth.hash_iterations
                )
                User.replace(username="admin", password_hash=password_hash).execute()

                logger.info("********************************************************")
                logger.info("********************************************************")
                logger.info("***    Reset admin password set in the config.       ***")
                logger.info(f"***    Password: {password}   ***")
                logger.info("********************************************************")
                logger.info("********************************************************")

    def start(self) -> None:
        parser = argparse.ArgumentParser(
            prog="Frigate",
            description="An NVR with realtime local object detection for IP cameras.",
        )
        parser.add_argument("--validate-config", action="store_true")
        args = parser.parse_args()

        self.init_logger()
        logger.info(f"Starting Frigate ({VERSION})")

        try:
            self.ensure_dirs()
            try:
                self.init_config()
            except Exception as e:
                print("*************************************************************")
                print("*************************************************************")
                print("***    Your config file is not valid!                     ***")
                print("***    Please check the docs at                           ***")
                print("***    https://docs.frigate.video/configuration/index     ***")
                print("*************************************************************")
                print("*************************************************************")
                print("***    Config Validation Errors                           ***")
                print("*************************************************************")
                if isinstance(e, ValidationError):
                    for error in e.errors():
                        location = ".".join(str(item) for item in error["loc"])
                        print(f"{location}: {error['msg']}")
                else:
                    print(e)
                    print(traceback.format_exc())
                print("*************************************************************")
                print("***    End Config Validation Errors                       ***")
                print("*************************************************************")
                self.log_process.terminate()
                sys.exit(1)
            if args.validate_config:
                print("*************************************************************")
                print("*** Your config file is valid.                            ***")
                print("*************************************************************")
                self.log_process.terminate()
                sys.exit(0)
            self.set_environment_vars()
            self.set_log_levels()
            self.init_queues()
            self.init_database()
            self.init_onvif()
            self.init_recording_manager()
            self.init_review_segment_manager()
            self.init_embeddings_manager()
            self.init_go2rtc()
            self.bind_database()
            self.check_db_data_migrations()
            self.init_inter_process_communicator()
            self.init_dispatcher()
        except Exception as e:
            print(e)
            self.log_process.terminate()
            sys.exit(1)
        self.start_detectors()
        self.start_video_output_processor()
        self.start_ptz_autotracker()
        self.init_historical_regions()
        self.start_detected_frames_processor()
        self.start_camera_processors()
        self.check_shm()
        self.start_camera_capture_processes()
        self.start_audio_processors()
        self.start_storage_maintainer()
        self.init_external_event_processor()
        self.start_stats_emitter()
        self.init_web_server()
        self.start_timeline_processor()
        self.start_event_processor()
        self.start_event_cleanup()
        self.start_record_cleanup()
        self.start_watchdog()
        self.init_auth()

        # Flask only listens for SIGINT, so we need to catch SIGTERM and send SIGINT
        def receiveSignal(signalNumber: int, frame: Optional[FrameType]) -> None:
            os.kill(os.getpid(), signal.SIGINT)

        signal.signal(signal.SIGTERM, receiveSignal)

        try:
            self.flask_app.run(host="127.0.0.1", port=5001, debug=False, threaded=True)
        except KeyboardInterrupt:
            pass

        logger.info("Flask has exited...")

        self.stop()

    def stop(self) -> None:
        logger.info("Stopping...")

        self.stop_event.set()

        # set an end_time on entries without an end_time before exiting
        Event.update(
            end_time=datetime.datetime.now().timestamp(), has_snapshot=False
        ).where(Event.end_time == None).execute()
        ReviewSegment.update(end_time=datetime.datetime.now().timestamp()).where(
            ReviewSegment.end_time == None
        ).execute()

        # stop the audio process
        if self.audio_process is not None:
            self.audio_process.terminate()
            self.audio_process.join()

        # ensure the capture processes are done
        for camera in self.camera_metrics.keys():
            capture_process = self.camera_metrics[camera]["capture_process"]
            if capture_process is not None:
                logger.info(f"Waiting for capture process for {camera} to stop")
                capture_process.terminate()
                capture_process.join()

        # ensure the camera processors are done
        for camera in self.camera_metrics.keys():
            camera_process = self.camera_metrics[camera]["process"]
            if camera_process is not None:
                logger.info(f"Waiting for process for {camera} to stop")
                camera_process.terminate()
                camera_process.join()
                logger.info(f"Closing frame queue for {camera}")
                frame_queue = self.camera_metrics[camera]["frame_queue"]
                empty_and_close_queue(frame_queue)

        # ensure the detectors are done
        for detector in self.detectors.values():
            detector.stop()

        empty_and_close_queue(self.detection_queue)
        logger.info("Detection queue closed")

        self.detected_frames_processor.join()
        empty_and_close_queue(self.detected_frames_queue)
        logger.info("Detected frames queue closed")

        self.timeline_processor.join()
        self.event_processor.join()
        empty_and_close_queue(self.timeline_queue)
        logger.info("Timeline queue closed")

        self.output_processor.terminate()
        self.output_processor.join()

        self.recording_process.terminate()
        self.recording_process.join()

        self.review_segment_process.terminate()
        self.review_segment_process.join()

        self.external_event_processor.stop()
        self.dispatcher.stop()
        self.ptz_autotracker_thread.join()

        self.event_cleanup.join()
        self.record_cleanup.join()
        self.stats_emitter.join()
        self.frigate_watchdog.join()
        self.db.stop()

        # Save embeddings stats to disk
        if self.embeddings:
            self.embeddings.save_stats()

        # Stop Communicators
        self.inter_process_communicator.stop()
        self.inter_config_updater.stop()
        self.inter_zmq_proxy.stop()

        while len(self.detection_shms) > 0:
            shm = self.detection_shms.pop()
            shm.close()
            shm.unlink()

        self.log_process.terminate()
        self.log_process.join()

        os._exit(os.EX_OK)
