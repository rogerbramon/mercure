"""
route_series.py
===============
Provides functions for routing/processing of series. For study-level processing, series will be pushed into study folders.
"""

# Standard python includes
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple, Union
from typing_extensions import Literal
import uuid
import json
import shutil
import daiquiri

# App-specific includes
import common.config as config
import common.rule_evaluation as rule_evaluation
import common.monitor as monitor
import common.helper as helper
import common.notification as notification
import common.log_helpers as log_helpers
from common.types import Rule
from common.constants import (
    mercure_defs,
    mercure_names,
    mercure_actions,
    mercure_rule,
    mercure_options,
    mercure_events,
)
from routing.generate_taskfile import create_series_task, create_study_task, update_study_task
from routing.common import generate_task_id

# Create local logger instance
logger = config.get_logger()


@log_helpers.clear_task_decorator
def route_series(task_id: str, series_UID: str) -> None:
    """
    Processes the series with the given series UID from the incoming folder.
    """
    logger.setTask(task_id)
    monitor.send_register_task(task_id, series_UID)

    lock_file = Path(config.mercure.incoming_folder + "/" + str(series_UID) + mercure_names.LOCK)
    if lock_file.exists():
        # Series is locked, so another instance might be working on it
        return

    # Create lock file in the incoming folder and prevent other instances from working on this series
    try:
        lock = helper.FileLock(lock_file)
    except FileExistsError:
        # Series likely already processed by other instance of router
        return
    except:
        # Can't create lock file, so something must be seriously wrong
        logger.error(f"Unable to create lock file {lock_file}", task_id)  # handle_error
        return

    logger.info(f"Processing series {series_UID}")
    fileList = []
    seriesPrefix = series_UID + mercure_defs.SEPARATOR

    # Collect all files belonging to the series
    for entry in os.scandir(config.mercure.incoming_folder):
        if entry.name.endswith(mercure_names.TAGS) and entry.name.startswith(seriesPrefix) and not entry.is_dir():
            stemName = entry.name[:-5]
            fileList.append(stemName)

    logger.info("DICOM files found: " + str(len(fileList)))
    if not len(fileList):
        logger.error(f"No tags files found for series {series_UID}", task_id)  # handle_error
        return

    # Use the tags file from the first slice for evaluating the routing rules
    tagsMasterFile = Path(config.mercure.incoming_folder + "/" + fileList[0] + mercure_names.TAGS)
    if not tagsMasterFile.exists():
        logger.error(f"Missing file! {tagsMasterFile.name}", task_id)  # handle_error
        return

    try:
        with open(tagsMasterFile, "r") as json_file:
            tagsList: Dict[str, str] = json.load(json_file)
    except Exception:
        logger.error(f"Invalid tag for series {series_UID}", task_id)  # handle_error
        return

    monitor.send_register_series(tagsList)

    # Now test the routing rules and evaluate which rules have been triggered. If one of the triggered
    # rules enforces discarding, discard_series will be True.
    discard_series = ""
    triggered_rules, discard_series = get_triggered_rules(task_id, tagsList)

    monitor.send_task_event(
        monitor.task_event.REGISTER, task_id, len(fileList), ", ".join(triggered_rules), "Registered series"
    )

    if (len(triggered_rules) == 0) or (discard_series):
        # If no routing rule has triggered or discarding has been enforced, discard the series
        push_series_complete(task_id, fileList, series_UID, "DISCARD", discard_series, False)
    else:
        # File handling strategy: If only one triggered rule, move files (faster than copying). If multiple rules, copy files
        push_series_studylevel(task_id, triggered_rules, fileList, series_UID, tagsList)
        push_series_serieslevel(task_id, triggered_rules, fileList, series_UID, tagsList)

        # If more than one rule has triggered, the series files need to be removed
        if len(triggered_rules) > 1:
            remove_series(task_id, fileList)

    try:
        lock.free()
    except Exception:
        logger.error(f"Unable to remove lock file {lock_file}", task_id)  # handle_error
        return


def get_triggered_rules(
    task_id: str, tagList: Dict[str, str]
) -> Tuple[Dict[str, Literal[True]], Union[Any, Literal[""]]]:
    """
    Evaluates the routing rules and returns a list with triggered rules.
    """
    triggered_rules: Dict[str, Literal[True]] = {}
    discard_rule = ""
    fallback_rule = ""

    # Iterate over all defined processing rules
    for current_rule in config.mercure.rules:
        try:
            rule: Rule = config.mercure.rules[current_rule]

            # Check if the current rule has been disabled
            if rule.get(mercure_rule.DISABLED, "False") == "True":
                continue

            # If the current rule is flagged as fallback rule, remember the name (to avoid having to iterate over the rules again)
            if rule.get(mercure_rule.FALLBACK, "False") == "True":
                fallback_rule = current_rule

            # Check if the current rule is triggered for the provided tag set
            if rule_evaluation.parse_rule(rule.get("rule", "False"), tagList):
                triggered_rules[current_rule] = True
                if rule.get(mercure_rule.ACTION, "") == mercure_actions.DISCARD:
                    discard_rule = current_rule
                    # If the triggered rule's action is to discard, stop further iteration over the rules
                    break

        except Exception as e:
            logger.error(f"Invalid rule found: {current_rule}", task_id)  # handle_error
            continue

    # If no rule has triggered but a fallback rule exists, then apply this rule
    if (len(triggered_rules) == 0) and (fallback_rule):
        triggered_rules[fallback_rule] = True
        if config.mercure.rules[fallback_rule].get(mercure_rule.ACTION, "") == mercure_actions.DISCARD:
            discard_rule = fallback_rule

    logger.info("Triggered rules:")
    logger.info(triggered_rules)
    return triggered_rules, discard_rule


def push_series_complete(
    task_id: str, file_list: List[str], series_UID: str, destination: str, discard_rule: str, copy_files: bool
) -> None:
    """
    Moves all files of the series into either the "discard" or "success" folders, which both are periodically cleared.
    """
    # Define the source and target folder. Use UUID as name for the target folder in the
    # discard or success directory to avoid collisions
    if destination == "DISCARD":
        destination_path = config.mercure.discard_folder + "/" + task_id
    else:
        destination_path = config.mercure.success_folder + "/" + task_id

    # Create subfolder in the discard directory and validate that is has been created
    try:
        os.mkdir(destination_path)
    except Exception:
        logger.error(f"Unable to create outgoing folder {destination_path}", task_id)  # handle_error
        return

    if not Path(destination_path).exists():
        logger.error(f"Creating discard folder not possible {destination_path}", task_id)  # handle_error
        return

    # Create lock file in destination folder (to prevent the cleaner module to work on the folder). Note that
    # the DICOM series in the incoming folder has already been locked in the parent function.
    lock_file = Path(destination_path) / mercure_names.LOCK
    try:
        lock = helper.FileLock(lock_file)
    except Exception:
        # Can't create lock file, so something must be seriously wrong
        logger.error(f"Unable to create lock file {destination_path}/{mercure_names.LOCK}", task_id)  # handle_error
        return

    if destination == "DISCARD":
        if discard_rule:
            info_text = "Discard by rule " + discard_rule
        else:
            info_text = "Discard by default."
        monitor.send_task_event(monitor.task_event.DISCARD, task_id, len(file_list), discard_rule or "", info_text)

    if not push_files(task_id, file_list, destination_path, copy_files):
        logger.error(f"Problem while moving completed files", task_id)  # handle_error

    operation_name = "MOVE"
    if copy_files:
        operation_name = "COPY"        
    monitor.send_task_event(monitor.task_event.MOVE, task_id, len(file_list), destination_path, operation_name)

    try:
        lock.free()
    except Exception:
        # Can't delete lock file, so something must be seriously wrong
        logger.error(f"Unable to remove lock file {lock_file}", task_id)  # handle_error
        return


def push_series_studylevel(
    task_id: str,
    triggered_rules: Dict[str, Literal[True]],
    file_list: List[str],
    series_UID: str,
    tags_list: Dict[str, str],
) -> None:
    """
    Prepeares study-level routing for the current series.
    """
    # Move series into individual study-level folder for every rule
    for current_rule in triggered_rules:
        if config.mercure.rules[current_rule].get("action_trigger", "series") == mercure_options.STUDY:
            first_series = False

            # Check if folder exists for buffering series until study completion. If not, create it
            study_UID = tags_list["StudyInstanceUID"]
            folder_name = config.mercure.studies_folder + "/" + study_UID + mercure_defs.SEPARATOR + current_rule
            target_folder = folder_name + "/"

            if not os.path.exists(folder_name):
                try:
                    os.mkdir(folder_name)
                    first_series = True
                except Exception:
                    logger.error(f"Unable to create study folder {folder_name}", task_id)  # handle_error
                    continue

            lock_file = Path(folder_name) / mercure_names.LOCK
            try:
                lock = helper.FileLock(lock_file)
            except:
                # Can't create lock file, so something must be seriously wrong
                logger.error(f"Unable to create lock file {lock_file}", task_id)  # handle_error
                continue

            if first_series:
                # Create task file with information on complete criteria
                new_task_id = generate_task_id()
                result = create_study_task(new_task_id, target_folder, triggered_rules, current_rule, study_UID, tags_list)
                monitor.send_task_event(monitor.task_event.ASSIGN, task_id, len(file_list), current_rule, "Created study task")
                monitor.send_task_event(monitor.task_event.DELEGATE, task_id, len(file_list), new_task_id, current_rule)
                monitor.send_task_event(monitor.task_event.ASSIGN, new_task_id, len(file_list), task_id, "Added series to study")
            else:
                # Add data from latest series to task file
                result, new_task_id = update_study_task(task_id, target_folder, triggered_rules, current_rule, study_UID, tags_list)
                monitor.send_task_event(monitor.task_event.ASSIGN, task_id, len(file_list), current_rule, "Added to study task")
                monitor.send_task_event(monitor.task_event.DELEGATE, task_id, len(file_list), new_task_id, current_rule)
                monitor.send_task_event(monitor.task_event.ASSIGN, new_task_id, len(file_list), task_id, "Added series to study")

            if not result:
                logger.error(f"Problem assigning series to study ", task_id)

            # Copy (or move) the files into the study folder
            push_files(task_id, file_list, folder_name, (len(triggered_rules) > 1))
            lock.free()


def push_series_serieslevel(
    task_id: str,
    triggered_rules: Dict[str, Literal[True]],
    file_list: List[str],
    series_UID: str,
    tags_list: Dict[str, str],
) -> None:
    """
    Prepeares all series-level routings for the current series.
    """
    push_serieslevel_routing(task_id, triggered_rules, file_list, series_UID, tags_list)
    push_serieslevel_processing(task_id, triggered_rules, file_list, series_UID, tags_list)
    push_serieslevel_notification(task_id, triggered_rules, file_list, series_UID, tags_list)


def push_serieslevel_routing(
    task_id: str,
    triggered_rules: Dict[str, Literal[True]],
    file_list: List[str],
    series_UID: str,
    tags_list: Dict[str, str],
) -> None:
    selected_targets: Dict[str, List[str]] = {}
    # Collect the dispatch-only targets to avoid that a series is sent twice to the
    # same target due to multiple targets triggered (note: this only makes sense for routing-only
    # series tasks, as study-level rules might have different completion criteria and tasks involving
    # processing steps might create different results so that they cannot be pooled)
    for current_rule in triggered_rules:
        rule_definition = config.mercure.rules[current_rule]
        if (
            rule_definition.get("action_trigger", "series") == mercure_options.SERIES
            and rule_definition.get("action") == mercure_actions.ROUTE
        ):
            target = rule_definition.get("target")
            if target:
                if not selected_targets.get(target):
                    selected_targets[target] = [current_rule]
                else:
                    selected_targets[target].append(current_rule)
            trigger_serieslevel_notification(current_rule, tags_list, mercure_events.RECEPTION, task_id)

    push_serieslevel_outgoing(task_id, triggered_rules, file_list, series_UID, tags_list, selected_targets)


def push_serieslevel_processing(
    task_id: str,
    triggered_rules: Dict[str, Literal[True]],
    file_list: List[str],
    series_UID: str,
    tags_list: Dict[str, str],
) -> bool:
    # Rules with action "processing" or "processing & routing" need to be processed separately (because the processing step can create varying results).
    # Thus, loop over all series-level rules that have triggered.
    for current_rule in triggered_rules:
        if config.mercure.rules[current_rule].get("action_trigger", mercure_options.SERIES) == mercure_options.SERIES:
            if (config.mercure.rules[current_rule].get("action", "") == mercure_actions.PROCESS) or (
                config.mercure.rules[current_rule].get("action", "") == mercure_actions.BOTH
            ):
                # Determine if the files should be copied or moved. If only one rule triggered, files can
                # safely be moved, otherwise files will be moved and removed in the end
                copy_files = True
                if len(triggered_rules) == 1:
                    copy_files = False
                new_task_id = generate_task_id()

                folder_name = config.mercure.processing_folder + "/" + new_task_id
                target_folder = folder_name + "/"

                # Create processing folder
                try:
                    os.mkdir(folder_name)
                except Exception:
                    logger.error(f"Unable to create outgoing folder {folder_name}", task_id)  # handle_error
                    return False

                if not Path(folder_name).exists():
                    logger.error(f"Creating folder not possible {folder_name}", task_id)  # handle_error
                    return False

                # Lock the case
                lock_file = Path(folder_name) / mercure_names.LOCK
                try:
                    lock = helper.FileLock(lock_file)
                except:
                    # Can't create lock file, so something must be seriously wrong
                    logger.error(f"Unable to create lock file {lock_file}", task_id)  # handle_error
                    return False

                monitor.send_task_event(monitor.task_event.DELEGATE, task_id, len(file_list), new_task_id, current_rule)

                    # Generate task file with processing information
                if create_series_task(
                    new_task_id, target_folder, triggered_rules, current_rule, series_UID, tags_list, ""
                ):
                    monitor.send_register_task(new_task_id, series_UID, task_id)
                else:
                    return False

                if not push_files(task_id, file_list, target_folder, copy_files):
                    logger.error(
                        f"Unable to push files into processing folder {target_folder}", task_id
                    )  # handle_error
                    return False

                try:
                    lock.free()
                except:
                    # Can't delete lock file, so something must be seriously wrong
                    logger.error(f"Unable to remove lock file {lock_file}", task_id)  # handle_error
                    return False

                trigger_serieslevel_notification(current_rule, tags_list, mercure_events.RECEPTION, task_id)
    return True


def push_serieslevel_notification(
    task_id: str,
    triggered_rules: Dict[str, Literal[True]],
    file_list: List[str],
    series_UID: str,
    tags_list: Dict[str, str],
) -> bool:
    notification_rules_count = 0

    for current_rule in triggered_rules:
        if config.mercure.rules[current_rule].get("action_trigger", mercure_options.SERIES) == mercure_options.SERIES:
            if config.mercure.rules[current_rule].get("action", "") == mercure_actions.NOTIFICATION:
                trigger_serieslevel_notification(current_rule, tags_list, mercure_events.RECEPTION, task_id)
                trigger_serieslevel_notification(current_rule, tags_list, mercure_events.COMPLETION, task_id)
                notification_rules_count += 1

    # If the current rule is "notification-only" and this is the only rule that has been
    # triggered, then discard the files so that they end up in the discard folder. If more
    # than one rule has triggered, the parent function will remove the files from the incoming
    # folder. However, it multiple rules have triggered and all such rules are notifications,
    # make a copy of the files into the discard folder, so that the files can be recovered
    if notification_rules_count > 0:
        if (len(triggered_rules) == 1) or (len(triggered_rules) == notification_rules_count):
            push_series_complete(task_id, file_list, series_UID, "SUCCESS", "", len(triggered_rules) > 1)

    return True


def push_serieslevel_outgoing(
    task_id: str,
    triggered_rules: Dict[str, Literal[True]],
    file_list: List[str],
    series_UID: str,
    tags_list: Dict[str, str],
    selected_targets: Dict[str, List[str]],
) -> None:
    """
    Move the DICOM files of the series to a separate subfolder for each target in the outgoing folder.
    """
    source_folder = config.mercure.incoming_folder + "/"

    # Determine if the files should be copied or moved. If only one rule triggered, files can
    # safely be moved, otherwise files will be moved and removed in the end
    move_operation = False
    if len(triggered_rules) == 1:
        move_operation = True

    for target in selected_targets:
        if not target in config.mercure.targets:
            logger.error(f"Invalid target selected {target}", task_id)  # handle_error
            continue

        new_task_id = generate_task_id()

        folder_name = config.mercure.outgoing_folder + "/" + new_task_id
        target_folder = folder_name + "/"

        try:
            os.mkdir(folder_name)
        except Exception:
            logger.error(f"Unable to create outgoing folder {folder_name}", task_id)  # handle_error
            return

        if not Path(folder_name).exists():
            logger.error(f"Creating folder not possible {folder_name}", task_id)  # handle_error
            return
        lock_file = Path(folder_name) / mercure_names.LOCK
        try:
            lock = helper.FileLock(lock_file)
        except:
            logger.error(f"Unable to create lock file {lock_file}", task_id)  # handle_error
            return

        # Collect the rules that triggered the dispatching to the current target
        target_rules: Dict[str, Literal[True]] = {}
        for rule in selected_targets[target]:
            target_rules[rule] = True

        # Generate task file with dispatch information
        if create_series_task(new_task_id, target_folder, target_rules, "", series_UID, tags_list, target):
            monitor.send_register_task(new_task_id, series_UID, task_id)
        else:
            continue

        monitor.send_task_event(monitor.task_event.DELEGATE, task_id, len(file_list), new_task_id, ", ".join(selected_targets[target]))

        operation: Callable
        if move_operation:
            operation = shutil.move
        else:
            operation = shutil.copy

        for entry in file_list:
            try:
                operation(source_folder + entry + mercure_names.DCM, target_folder + entry + mercure_names.DCM)
                operation(source_folder + entry + mercure_names.TAGS, target_folder + entry + mercure_names.TAGS)
            except Exception:
                logger.error(  # handle_error
                    f"Problem while pushing file to outgoing {entry}\nSource folder {source_folder}\nTarget folder {target_folder}",
                    task_id,
                )

        if move_operation:
            monitor.send_task_event(monitor.task_event.MOVE, task_id, len(file_list), target_folder, "Moved files")
        else:
            monitor.send_task_event(monitor.task_event.COPY, task_id, len(file_list), target_folder, "Copied files")

        try:
            lock.free()
        except Exception:
            # Can't delete lock file, so something must be seriously wrong
            logger.error(f"Unable to remove lock file {lock_file}", task_id)  # handle_error
            return


def push_files(task_id: str, file_list: List[str], target_path: str, copy_files: bool) -> bool:
    """
    Copies or moves the given files to the target path. If copy_files is True, files are copied, otherwise moved.
    Note that this function does not create a lock file (this needs to be done by the calling function).
    """
    operation: Callable
    if copy_files == False:
        operation = shutil.move
    else:
        operation = shutil.copy

    source_folder = config.mercure.incoming_folder + "/"
    target_folder = target_path + "/"

    for entry in file_list:
        try:
            operation(source_folder + entry + mercure_names.DCM, target_folder + entry + mercure_names.DCM)
            operation(source_folder + entry + mercure_names.TAGS, target_folder + entry + mercure_names.TAGS)
        except Exception:
            logger.error(  # handle_error
                f"Problem while pushing file to outgoing {entry}\n Source folder {source_folder}\nTarget folder {target_folder}",
                task_id,
            )
            return False

    if copy_files == False:
        monitor.send_task_event(monitor.task_event.MOVE, task_id, len(file_list), target_path, "Moved files")
    else:
        monitor.send_task_event(monitor.task_event.COPY, task_id, len(file_list), target_path, "Copied files")

    return True


def remove_series(task_id: str, file_list: List[str]) -> bool:
    """
    Deletes the given files from the incoming folder.
    """
    source_folder = config.mercure.incoming_folder + "/"
    for entry in file_list:
        try:
            os.remove(source_folder + entry + mercure_names.TAGS)
            os.remove(source_folder + entry + mercure_names.DCM)
        except Exception:
            logger.error(f"Error while removing file {entry}", task_id)  # handle_error
            return False
 
    monitor.send_task_event(monitor.task_event.REMOVE, task_id, len(file_list), "", "Removed duplicate files")

    return True


def route_error_files() -> None:
    """
    Looks for error files, moves these files and the corresponding DICOM files to the error folder,
    and sends an alert to the bookkeeper instance.
    """
    error_files_found = 0

    for entry in os.scandir(config.mercure.incoming_folder):
        if entry.name.endswith(mercure_names.ERROR) and not entry.is_dir():
            # Check if a lock file exists. If not, create one.
            lock_file = Path(config.mercure.incoming_folder + "/" + entry.name + mercure_names.LOCK)
            if lock_file.exists():
                continue
            try:
                lock = helper.FileLock(lock_file)
            except:
                continue

            logger.error(f"Found incoming error file {entry.name}")
            error_files_found += 1

            shutil.move(
                config.mercure.incoming_folder + "/" + entry.name,
                config.mercure.error_folder + "/" + entry.name,
            )

            dicom_filename = entry.name[:-6]
            dicom_file = Path(config.mercure.incoming_folder + "/" + dicom_filename)
            if dicom_file.exists():
                shutil.move(
                    config.mercure.incoming_folder + "/" + dicom_filename,
                    config.mercure.error_folder + "/" + dicom_filename,
                )

            lock.free()

    if error_files_found > 0:
        monitor.send_event(
            monitor.m_events.PROCESSING, monitor.severity.ERROR, f"Error parsing {error_files_found} incoming files"
        )
    return


def trigger_serieslevel_notification(current_rule: str, tags_list: Dict[str, str], event, task_id: str) -> None:
    notification_type = ''

    if event == mercure_events.RECEPTION:
        if config.mercure.rules[current_rule].notification_trigger_reception == "True":
            notification.send_webhook(
                config.mercure.rules[current_rule].get("notification_webhook", ""),
                config.mercure.rules[current_rule].get("notification_payload", ""),
                mercure_events.RECEPTION,
                current_rule,
                task_id,
            )
            notification_type = 'RECEPTION'

    if event == mercure_events.COMPLETION:
        if config.mercure.rules[current_rule].notification_trigger_completion == "True":
            notification.send_webhook(
                config.mercure.rules[current_rule].get("notification_webhook", ""),
                config.mercure.rules[current_rule].get("notification_payload", ""),
                mercure_events.COMPLETION,
                current_rule,
                task_id,
            )
            notification_type = 'COMPLETION'            

    if event == mercure_events.ERROR:
        if config.mercure.rules[current_rule].notification_trigger_error == "True":
            notification.send_webhook(
                config.mercure.rules[current_rule].get("notification_webhook", ""),
                config.mercure.rules[current_rule].get("notification_payload", ""),
                mercure_events.ERROR,
                current_rule,
                task_id,
            )
            notification_type = 'ERROR'

    if notification_type and config.mercure.rules[current_rule].get("notification_webhook", ""):
        monitor.send_task_event(
            monitor.task_event.NOTIFICATION, task_id, 0, 
            config.mercure.rules[current_rule].get("notification_webhook", ""), 
            "Announced " + notification_type
        )
