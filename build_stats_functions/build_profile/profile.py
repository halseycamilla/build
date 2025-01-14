# Copyright 2023 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# =============================================================================
"""Python Code for grabbing stats from build profile to import into BigQuery."""
import datetime
import gzip
import json
import logging
import os
import re
import tempfile

import functions_framework
from google.cloud import bigquery
from google.cloud import logging
from google.cloud import storage


class UnableToUnzipFileError(Exception):
  """Unable to unzip input file."""


class IncorrectFileTypeError(Exception):
  """This file is not correctly formatted in JSON."""


class MissingTopLevelError(Exception):
  """The build profile is missing the top-level object."""


class IncorrectProfileFormatError(Exception):
  """This file is not in the correct Build Profile format."""


class MissingFirstLineError(Exception):
  """This file is missing top level data."""


@functions_framework.cloud_event
def main(cloud_event: any):
  """Entry point function that is triggered by uploading build profile.

  Gathers stats from build profile file and sends to BigQuery. Goes through each
  thread in the file and for each event in the thread extracts relevant info and
  writes as an object into bigquery

  Args:
    cloud_event: the google cloud event that triggered the function
  """
  try:
    client = logging.Client()
    logger = client.logger("build-profile-testing")
    logger.log_text(
        "Successfully connected to GCP Logging Client", severity="INFO"
    )
  except logging.Error:
    logger.log_text(
        "Unable to connect to GCP Logging client", severity="WARNING"
    )
    return
  try:
    STORAGE_BUCKET = os.environ.get("STORAGE_BUCKET")
    TABLE_ID = os.environ.get("TABLE_ID")
  except Exception as exc:
    logger.log_text("Unable to get environment variables", severity="WARNING")
    raise exc
  try:
    client = logging.Client()
    logger = client.logger("build-profile-testing")
    logger.log_text(
        "Successfully connected to GCP Logging Client", severity="INFO"
    )
  except logging.Error:
    logger.log_text(
        "Unable to connect to GCP Logging client", severity="WARNING"
    )
    return
  data = cloud_event.data
  if "name" not in data:
    logger.log_text("No filename was found", severity="WARNING")
    return
  file_name = data["name"]
  if not check_path(file_name):
    logger.log_text(
        "The current file is not a build profile according to its path",
        severity="INFO",
    )
    return
  logger.log_text("Build profile found", severity="INFO")
  r = re.search("/[0-9].*", file_name)
  if not r:
    logger.log_text("Job name is not valid", severity="WARNING)
    return
  index = r.span()[0]
  job = file_name[0:index]
  try:
    storage_client = storage.Client()
    bucket = storage_client.get_bucket(STORAGE_BUCKET)
    data_blob = bucket.get_blob(file_name)
  except Exception as exc:
    logger.log_text(
        "Unable to access Cloud Storage client or storage bucket",
        severity="WARNING",
    )
    raise exc
  res = get_data(data_blob)
  build_id = res[0]
  threads = res[1]
  all_threads = get_times(threads)
  if not all_threads:
    logger.log_text(
        "Profile had no completed action events or wasn't in correct format",
        severity="WARNING",
    )
    return
  try:
    bigquery_client = bigquery.Client()
    objs = create_event_objects(all_threads)
    for obj in objs:
      obj["BUILD_ID"] = build_id
      obj["JOB_NAME"] = job
      obj["DATE_CREATED"] = str(datetime.date.today())
      bigquery_client.insert_rows_json(TABLE_ID, [obj])
  except Exception as exc:
    logger.log_text("Unable to access Bigquery client", severity="WARNING")
    raise exc


def check_path(file_name: str):
  regex = "prod/tensorflow/rel.*/profile.json.gz"
  check_file = re.match(regex, file_name)
  return check_file


def get_data(data_blob: storage.Blob):
  """Goes through each line in blob file and converts strings to dictionary object.

  Args:
    data_blob: the blob file that was uploaded in the triggering cloud event

  Raises:
    MissingTopLevelError: Doesn't have top level object
    IncorrectProfileFormatError: Missing required fields for a profile
    IncorrectFileTypeError: One or more lines is not in correct JSON format
    UnableToUnzipFile: Unable to unzip input file
  Returns:
    A dict mapping keys of thread ids to an array of events in that thread
  """
  temp_dir = tempfile.TemporaryDirectory()
  temp_file = temp_dir.name + "/profile.json.gz"
  data_blob.download_to_filename(temp_file)
  threads = {}
  cats = {}
  try:
    with gzip.open(temp_file, "rb") as file:
      try:
        data = json.load(file)
      except Exception as exc:
        raise IncorrectProfileFormatError() from exc
      if "otherData" not in data or "traceEvents" not in data:
        raise MissingTopLevelError()
      if "build_id" not in data["otherData"]:
        raise IncorrectProfileFormatError()
      build_id = data["otherData"]["build_id"]
      for line in data["traceEvents"]:
        if "cat" in line:
          if line["cat"] not in cats:
            cats[line["cat"]] = []
          cats[line["cat"]].append(line["name"])
        if "tid" not in line:
          raise IncorrectProfileFormatError()
        a = line["tid"]
        if a not in threads:
          threads[a] = []
        threads[a].append(line)
  except Exception as exc:
    raise UnableToUnzipFileError() from exc
  res = [build_id, threads]
  return res


def get_times(threads: dict[str, list[str]]):
  """For each event in thread keep track of total time it takes.

  and its category
  Args:
    threads: dictionary of {thread id: [events in thread]}

  Raises:
    IncorrectProfileFormatError: Incorrect format for profile format
  Returns:
    A dict mapping type of event to total time event took
  """
  # For each thread create a dictionary of {eventtype: sum of eventtype times}
  # Also keep an overall dictionary of all of the threads
  all_threads = {}
  categories = {}
  for thread in threads:
    total_time = 0
    all_events = []
    event_times = {}
    seen = set()
    for event in threads[thread]:
      if "ph" not in event:
        raise IncorrectProfileFormatError()
      if event["ph"] == "X":
        if "ts" not in event or "dur" not in event:
          raise IncorrectProfileFormatError()
          # Only look at complete events (indicated by phase X)
          # To calculate self time want to get the duration
          # and subtract the time of any child jobs.
          # Child events show up before the current event however can be out of
          # order overall so hence loop through all previous events in the file.
          # If the time stamps overlap we see it is a child event.
          # NOTE: In rare occassions there won't be an overlap showing up but
          # this only throws off overall self time of event by 10ths of
          # a second.
        self_time = event["dur"]
        categories[event["name"]] = event["cat"]
        if total_time > 0:
          low = event["ts"]
          high = low + event["dur"]
          for i in range(len(all_events) - 1, -1, -1):
            curr = all_events[i]
            if (
                curr["ts"] > low
                and curr["ts"] < high
                and str(curr) not in seen
                and curr["dur"] < event["dur"]
            ):
              seen.add(str(curr))
              self_time -= curr["dur"]
        all_events.append(event)
        total_time += self_time
        if "name" not in event:
          raise IncorrectProfileFormatError()
        event_type = event["name"]
        if event_type not in event_times:
          event_times[event_type] = 0
        event_times[event_type] += self_time
    all_threads[thread] = event_times
  return [all_threads, categories]


def create_event_objects(data: list[dict[str, dict[str, int]]]):
  """For each event in each thread create an object.

  Args:
    data: list of threads and categories

  Returns:
    A list of all events as objects
  """
  all_threads = data[0]
  categories = data[1]
  objs = []
  for line in all_threads:
    for data in all_threads[line]:
      ev = {}
      ev["EVENT_NAME"] = data
      ev["CATEGORY"] = categories[data]
      ev["THREAD"] = line
      ev["TIME_TAKEN"] = float(all_threads[line][data]) / 1000000
      objs.append(ev)
  return objs
