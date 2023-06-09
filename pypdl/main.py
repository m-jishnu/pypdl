import json
import threading
import time
from collections import deque
from datetime import datetime
from math import inf
from pathlib import Path
from typing import Callable, Dict, List, Optional, Union

import requests
from reprint import output

from .utls import Multidown, Singledown, timestring


class Downloader:
    def __init__(self, StopEvent: Optional[threading.Event] = None, headers: Optional[Dict[str, str]] = None):
        """
        Initializes the Downloader object.

        Parameters:
            StopEvent (threading.Event): Event to stop the download.
            headers (dict): User headers to be used in the download request.
        """
        # private attributes
        # keep track of recent download speed
        self._recent = deque([0] * 12, maxlen=12)
        self._dic: Dict[Union[str, int], Union[int, bool, str, Dict[str, Union[str, int, bool]]]] = {}  # dictionary to keep track of download progress
        self._workers: List[Union[Multidown, Singledown]] = []  # list of download worker threads
        self._Error = threading.Event()  # event to signal any download errors

        # public attributes
        self.totalMB = 0  # total download size in MB
        self.progress = 0  # download progress percentage
        self.speed = 0  # download speed in MB/s
        self.download_mode = ""  # download mode: single-threaded or multi-threaded
        self.time_spent: Optional[float] = None  # time spent downloading
        self.doneMB = 0  # amount of data downloaded in MB
        self.eta = "99:59:59"  # estimated time remaining for download completion
        self.remaining = 0  # amount of data remaining to be downloaded
        self.Stop = StopEvent if StopEvent else threading.Event() # event to signal download stop
        self.headers = headers if headers else {} # user-defined headers
        self.Failed = False  # flag to indicate if download failure

    def download(self, url: str, filepath: str, num_connections: int, display: bool, multithread: bool):
        """
        Download a file from the given URL.

        Parameters:
            url (str): The URL of the file to download.
            filepath (str): The file path to save the download.
            num_connections (int): The number of connections to use for a multi-threaded download.
            display (bool): Whether to display download progress.
            multithread (bool): Whether to use multi-threaded download.
        """
        # progress file to keep track of download progress
        json_file = Path(filepath + ".progress.json")
        threads = []
        f_path = str(filepath)
        # get the header information for the file
        head = requests.head(url, timeout=20)
        # get the total size of the file from the header
        total = int(head.headers.get("content-length"))
        self.totalMB = total / 1048576  # 1MB = 1048576 bytes (size in MB)
        started = datetime.now()
        singlethread = False

        # adjust the number of connections for small files
        if self.totalMB < 50:
            num_connections = 5 if num_connections > 5 else num_connections

        # if no range available in header or no size from header, use single thread
        if not total or not head.headers.get("accept-ranges") or not multithread:
            # create single-threaded download object
            sd = Singledown(url, f_path, self.Stop, self._Error, self.headers)
            # create single download worker thread
            th = threading.Thread(target=sd.worker)
            self._workers.append(sd)
            th.start()
            total = inf if not total else total
            singlethread = True
        else:
            # multiple threads possible
            if json_file.exists():
                # load the progress from the progress file
                # the object_hook converts the key strings whose value is int to type int
                try:
                    progress = json.loads(
                        json_file.read_text(),
                        object_hook=lambda d: {
                            int(k) if k.isdigit() else k: v for k, v in d.items()},
                    )
                except:
                    pass
            segment = total / num_connections
            self._dic["total"] = total
            self._dic["connections"] = num_connections
            self._dic["paused"] = False
            for i in range(num_connections):
                try:
                    # try to use progress file to resume download
                    start = progress[i]["start"]
                    end = progress[i]["end"]
                    curr = progress[i]["curr"]
                    size = progress[i]["size"]
                except:
                    # if not able to use progress file, then calculate the start, end, curr, and size
                    # calculate the beginning byte offset by multiplying the segment by num_connections.
                    start = int(segment * i)
                    # here end is the ((segment * next part) - 1 byte) since the last byte is downloaded by next part except for the last part
                    end = int(segment * (i + 1)) - (i != num_connections - 1)
                    curr = start
                    size = end - start + (i != num_connections - 1)

                self._dic[i] = {
                    "start": start,
                    "curr": curr,
                    "end": end,
                    "filepath": f"{filepath}.{i}.part",
                    "size": size,
                    "url": url,
                    "completed": False,
                }
                # create multidownload object for each connection
                md = Multidown(self._dic, i, self.Stop,
                            self._Error, self.headers)
                # create worker thread for each connection
                th = threading.Thread(target=md.worker)
                threads.append(th)
                th.start()
                self._workers.append(md)

            # save the progress to the progress file
            if not singlethread:
                json_file.write_text(json.dumps(self._dic, indent=4))

        downloaded = 0
        interval = 0.15
        self.download_mode = "Multi-Threaded" if not singlethread else "Single-Threaded"
        # use reprint library to print dynamic progress output
        with output(initial_len=5, interval=0) as dynamic_print:
            while True:
                if not singlethread:
                    # save progress to progress file
                    json_file.write_text(json.dumps(self._dic, indent=4))
                # check if all workers have completed
                status = sum(i.completed for i in self._workers)
                # get the total amount of data downloaded
                downloaded = sum(i.curr for i in self._workers)
                self.doneMB = downloaded / 1048576
                # keep track of recent download speeds
                self._recent.append(downloaded)
                try:
                    # calculate download progress percentage
                    self.progress = int(100 * downloaded / total)
                except ZeroDivisionError:
                    self.progress = 0

                # calculate download speed
                recent_speed = len([i for i in self._recent if i])
                if not recent_speed:
                    self.speed = 0
                else:
                    recent = list(self._recent)[12 - recent_speed:]
                    if len(recent) == 1:
                        self.speed = recent[0] / 1048576 / interval
                    else:
                        diff = [b - a for a, b in zip(recent, recent[1:])]
                        self.speed = sum(diff) / len(diff) / 1048576 / interval

                # calculate estimated time remaining for download completion
                self.remaining = self.totalMB - self.doneMB
                if self.speed and total != inf:
                    self.eta = timestring(self.remaining / self.speed)
                else:
                    self.eta = "99:59:59"

                # print dynamic progress output
                if display:
                    if not singlethread:
                        dynamic_print[0] = (
                            "[{0}{1}] {2}".format(
                                "\u2588" * self.progress,
                                "\u00b7" * (100 - self.progress),
                                str(self.progress),
                            )
                            + "%"
                        )
                        dynamic_print[1] = f"Total: {self.totalMB:.2f} MB, Download Mode: {self.download_mode}, Speed: {self.speed:.2f} MB/s, ETA: {self.eta}"
                    else:
                        dynamic_print[0] = "Downloading..."
                        dynamic_print[1] = f"Downloaded: {self.doneMB:.2f} MB, Download Mode: {self.download_mode}, Speed: {self.speed:.2f} MB/s"

                # check if download has been stopped or if an error has occurred
                if self.Stop.is_set() or self._Error.is_set():
                    self._dic["paused"] = True
                    if not singlethread:
                        # save progress to progress file
                        json_file.write_text(json.dumps(self._dic, indent=4))
                    break

                # check if all workers have completed
                if status == len(self._workers):
                    if not singlethread:
                        # combine the parts together
                        BLOCKSIZE = 4096
                        BLOCKS = 1024
                        CHUNKSIZE = BLOCKSIZE * BLOCKS
                        with open(f_path, "wb") as dest:
                            for i in range(num_connections):
                                file_ = f"{filepath}.{i}.part"
                                with open(file_, "rb") as f:
                                    while True:
                                        chunk = f.read(CHUNKSIZE)
                                        if chunk:
                                            dest.write(chunk)
                                        else:
                                            break
                                Path(file_).unlink()
                        # delete the progress file
                        json_file.unlink()
                    break
                time.sleep(interval)

        ended = datetime.now()
        self.time_spent = (ended - started).total_seconds()

        # print download result
        if display:
            if self.Stop.is_set():
                print("Task interrupted!")
            print(f"Time elapsed: {timestring(self.time_spent)}")

    def stop(self):
        """
        Stop the download process.
        """
        self.Stop.set()

    def start(
        self,
        url: str,
        filepath: str,
        num_connections: int = 10,
        display: bool = True,
        multithread: bool = True,
        block: bool = True,
        retries: int = 0,
        retry_func: Optional[Callable[[], str]] = None,
    ):
        """
        Start the download process.

        Parameters:
            url (str): The download URL.
            filepath (str): The file path to save the download.
            num_connections (int): The number of connections to use for a multi-threaded download.
            display (bool): Whether to display download progress.
            multithread (bool): Whether to use multi-threaded download.
            block (bool): Whether to block until the download is complete.
            retries (int): The number of times to retry the download in case of an error.
            retry_func (function): A function to call to get a new download URL in case of an error.
        """

        def start_thread():
            try:
                # start the download, not using "try" since all expected errors and will trigger error event
                self.download(url, filepath, num_connections,
                            display, multithread)
                # retry the download if there are errors
                for _ in range(retries):
                    if self._Error.is_set():
                        time.sleep(3)
                        # reset the downloader object
                        self.__init__(self.Stop, self.headers)

                        # get a new download URL to retry
                        _url = url
                        if callable(retry_func):
                            try:
                                _url = retry_func()
                            except Exception as e:
                                print(
                                    f"Retry function Error: ({e.__class__.__name__}, {e})")

                        if display:
                            print("retrying...")
                        # restart the download
                        self.download(_url, filepath, num_connections,
                                    display, multithread)
                    else:
                        break
            # if there's an error, set the error event and print the error message
            except Exception as e:
                print(f"Download Error: ({e.__class__.__name__}, {e})")
                self._Error.set()

            # if error flag is set, set the failed flag to True
            if self._Error.is_set():
                self.Failed = True
                print("Download Failed!")

        # Initialize the downloader with stop Event
        self.__init__(self.Stop, self.headers)
        self.Stop.clear()
        # Start the download process in a new thread
        th = threading.Thread(target=start_thread)
        th.start()

        # Block the current thread until the download is complete, if necessary
        if block:
            th.join()
