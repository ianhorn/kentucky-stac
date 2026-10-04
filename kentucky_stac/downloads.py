"""Download tiles to disk via QGIS's network stack (so proxy and CA settings apply, including behind
HTTPS-inspecting antivirus).

A tile whose URL is s3://... is read as its public https address, or -- when AWS credentials are
configured (see s3.py) -- through GDAL's /vsis3/, which signs the request and so also reaches private and
requester-pays buckets. An Azure blob URL (Planetary Computer) gets a free SAS token attached (signing.py)."""

from __future__ import annotations

import os
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from qgis.core import QgsApplication, QgsBlockingNetworkRequest, QgsFileDownloader, QgsTask
from qgis.PyQt.QtCore import QObject, QTimer, QUrl, pyqtSignal
from qgis.PyQt.QtNetwork import QNetworkRequest

from . import s3, signing
from .catalog import primary_asset
from .gdal_setup import setup_gdal
from .stac import Item

MAX_PARALLEL_DOWNLOADS = 3
SIZE_LOOKUP_WORKERS = 8
MIN_DOWNLOAD_CONCURRENCY = 1
MAX_DOWNLOAD_CONCURRENCY = 16


def default_concurrency() -> int:
    """75% of logical cores, the same default the old ArcGIS Pro add-in's "Parallel Downloads"
    option used -- a download is network-bound, not CPU-bound, but this matches the efficiency the
    user asked to carry over rather than inventing a different heuristic."""
    cores = os.cpu_count() or 4
    return min(MAX_DOWNLOAD_CONCURRENCY, max(MIN_DOWNLOAD_CONCURRENCY, round(cores * 0.75)))


@dataclass(frozen=True)
class DownloadJob:
    name: str  # tile id, for messages
    url: str
    dest: str  # final path; written as dest + ".part" and renamed when complete
    bbox: Optional[Tuple[float, float, float, float]] = None  # the tile's lon/lat footprint from the catalog
    item: Optional[Item] = None  # the originating STAC item, for building a combined VPC afterward


def filename_from_href(href: str) -> str:
    name = os.path.basename(urllib.parse.unquote(urllib.parse.urlparse(href).path))
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name) or "download"


def plan_downloads(items: Iterable[Item], lidar: bool, folder: str) -> Tuple[List[DownloadJob], List[Item]]:
    """One job per tile that has a downloadable asset, all into `folder`, plus the tiles that have
    none. Two tiles that would land on the same file name share a single job."""
    jobs: Dict[str, DownloadJob] = {}
    missing: List[Item] = []
    for item in items:
        asset = primary_asset(item, lidar)
        if asset is None or not asset.href:
            missing.append(item)
            continue
        dest = os.path.join(folder, filename_from_href(asset.href))
        bbox = tuple(float(v) for v in item.bbox[:4]) if item.bbox and len(item.bbox) >= 4 else None
        jobs.setdefault(dest, DownloadJob(item.id, asset.href, dest, bbox, item))
    return list(jobs.values()), missing


def _remove_when_free(path: str, attempts: int = 50, delay_ms: int = 100):
    """Delete a leftover partial file. On Windows the downloader can still hold the file open for a
    moment after it reports it has exited, so a failed delete is retried rather than ignored."""
    try:
        os.remove(path)
    except FileNotFoundError:
        return
    except OSError:
        if attempts > 1:
            QTimer.singleShot(delay_ms, lambda: _remove_when_free(path, attempts - 1, delay_ms))


_S3_HINT = (
    " (an s3:// file that isn't public needs AWS credentials: set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY, "
    "or create ~/.aws/credentials, then restart QGIS)"
)


def _signed_size(url: str) -> Optional[int]:
    from osgeo import gdal

    setup_gdal()  # GDAL's own libcurl needs the certificate-trust fix to reach S3 behind HTTPS inspection
    gdal.SetThreadLocalConfigOption("AWS_REQUEST_PAYER", "requester")
    try:
        stat = gdal.VSIStatL(s3.vsis3_path(url))
        return int(stat.size) if stat else None
    finally:
        gdal.SetThreadLocalConfigOption("AWS_REQUEST_PAYER", None)


def head_size(url: str) -> Optional[int]:
    """Content-Length from a HEAD request, or None. Safe to call from worker threads."""
    if s3.is_s3(url) and s3.credentials_available():
        try:
            return _signed_size(url)
        except Exception:
            return None
    url = signing.fetchable_url(url)  # s3:// -> https, Azure blob -> signed
    try:
        blocking = QgsBlockingNetworkRequest()
        error = blocking.head(QNetworkRequest(QUrl(url)))
        if int(getattr(error, "value", error)) != 0:
            return None
        value = bytes(blocking.reply().rawHeader(b"Content-Length"))
        return int(value) if value else None
    except Exception:
        return None


class SizesTask(QgsTask):
    """Look up file sizes for a set of URLs. `callback({url: size or None})` runs on the main
    thread unless the task was cancelled."""

    def __init__(self, urls: List[str], callback: Callable[[Dict[str, Optional[int]]], None]):
        super().__init__(f"Checking size of {len(urls)} file{'s' if len(urls) != 1 else ''}")
        self._urls = urls
        self._callback = callback
        self.sizes: Dict[str, Optional[int]] = {}

    def run(self) -> bool:
        with ThreadPoolExecutor(max_workers=SIZE_LOOKUP_WORKERS) as pool:
            for url, size in zip(self._urls, pool.map(head_size, self._urls)):
                self.sizes[url] = size
                self.setProgress(100.0 * len(self.sizes) / len(self._urls))
                if self.isCanceled():
                    return False
        return True

    def finished(self, result: bool) -> None:
        if not self.isCanceled():
            self._callback(self.sizes)


class _S3FetchTask(QgsTask):
    """Copy one s3:// file to disk through GDAL's /vsis3/ (signed with the user's AWS credentials,
    requester-pays header included). `done_bytes` / `total_bytes` are read by the manager on progress."""

    CHUNK = 1 << 20

    def __init__(self, url: str, dest: str):
        super().__init__(f"Downloading {os.path.basename(dest)}")
        self._src = s3.vsis3_path(url)
        self._dest = dest
        self.done_bytes = 0
        self.total_bytes = 0
        self.error: Optional[str] = None

    def run(self) -> bool:
        from osgeo import gdal

        setup_gdal()
        gdal.SetThreadLocalConfigOption("AWS_REQUEST_PAYER", "requester")
        try:
            stat = gdal.VSIStatL(self._src)
            self.total_bytes = int(stat.size) if stat else 0
            handle = gdal.VSIFOpenL(self._src, "rb")
            if handle is None:
                self.error = gdal.VSIGetLastErrorMsg() or "could not open the file (access denied?)"
                return False
            try:
                with open(self._dest, "wb") as out:
                    while True:
                        if self.isCanceled():
                            return False
                        data = gdal.VSIFReadL(1, self.CHUNK, handle)
                        if not data:
                            break
                        out.write(data)
                        self.done_bytes += len(data)
                        if self.total_bytes:
                            self.setProgress(100.0 * self.done_bytes / self.total_bytes)
            finally:
                gdal.VSIFCloseL(handle)
            if self.total_bytes and self.done_bytes != self.total_bytes:
                self.error = f"incomplete download ({self.done_bytes} of {self.total_bytes} bytes)"
                return False
            return True
        except Exception as e:
            self.error = str(e)
            return False
        finally:
            gdal.SetThreadLocalConfigOption("AWS_REQUEST_PAYER", None)


class _State:
    def __init__(self, job: DownloadJob, total: Optional[int]):
        self.job = job
        self.received = 0
        self.total = total or 0
        self.completed = False
        self.errors: List[str] = []
        self.downloader: Optional[QgsFileDownloader] = None
        self.task: Optional[_S3FetchTask] = None
        self.signed = False


class DownloadManager(QObject):
    """Downloads jobs a few at a time. Each file streams to `<dest>.part` and is renamed on success,
    so a final-named file is always complete.

    progress(files_done, files_total, percent); finished(completed_jobs, failed[(name, error)], cancelled)
    """

    progress = pyqtSignal(int, int, int)
    finished = pyqtSignal(list, list, bool)

    def __init__(self, jobs: List[DownloadJob], sizes: Optional[Dict[str, Optional[int]]] = None,
                 max_parallel: int = MAX_PARALLEL_DOWNLOADS, parent=None):
        super().__init__(parent)
        sizes = sizes or {}
        self._queue: List[_State] = [_State(j, sizes.get(j.url)) for j in jobs]
        self._all = list(self._queue)
        self._active: List[_State] = []
        self._max_parallel = max_parallel
        self._completed: List[DownloadJob] = []
        self._failed: List[Tuple[str, str]] = []
        self._cancelled = False
        self._finished_emitted = False

    @property
    def running(self) -> bool:
        return not self._finished_emitted

    def start(self):
        self._pump()

    def cancel(self):
        self._cancelled = True
        self._queue.clear()
        for state in list(self._active):
            if state.downloader is not None:
                state.downloader.cancelDownload()
            if state.task is not None:
                state.task.cancel()
        self._maybe_finish()

    def _pump(self):
        while not self._cancelled and self._queue and len(self._active) < self._max_parallel:
            self._start(self._queue.pop(0))
        self._maybe_finish()

    def _start(self, state: _State):
        job = state.job
        part = job.dest + ".part"
        try:
            os.makedirs(os.path.dirname(job.dest), exist_ok=True)
            if os.path.exists(part):
                os.remove(part)
        except OSError as e:
            self._failed.append((job.name, str(e)))
            return
        self._active.append(state)
        if s3.is_s3(job.url) and s3.credentials_available():
            self._start_signed(state, part)
            return
        downloader = QgsFileDownloader(QUrl(signing.fetchable_url(job.url)), part, "", True)
        # The manager owns the downloader (Qt parent) and keeps the Python wrapper alive. Letting the
        # wrapper be garbage-collected from inside the downloader's own downloadExited signal frees
        # the C++ object mid-emit and crashes QGIS.
        downloader.setParent(self)
        state.downloader = downloader
        downloader.downloadProgress.connect(lambda received, total, s=state: self._on_progress(s, received, total))
        downloader.downloadCompleted.connect(lambda _url, s=state: setattr(s, "completed", True))
        downloader.downloadError.connect(lambda errors, s=state: s.errors.extend(str(e) for e in errors))
        downloader.downloadExited.connect(lambda s=state: self._on_exited(s))
        downloader.startDownload()

    def _start_signed(self, state: _State, part: str):
        state.signed = True
        task = _S3FetchTask(state.job.url, part)
        state.task = task  # kept on the state so the Python wrapper outlives the task's own signals
        task.progressChanged.connect(lambda _pct, s=state, t=task: self._on_signed_progress(s, t))
        task.taskCompleted.connect(lambda s=state: (setattr(s, "completed", True), self._on_exited(s)))
        task.taskTerminated.connect(lambda s=state, t=task: (s.errors.append(t.error) if t.error else None, self._on_exited(s)))
        QgsApplication.taskManager().addTask(task)

    def _on_signed_progress(self, state: _State, task: _S3FetchTask):
        state.received = task.done_bytes
        if task.total_bytes:
            state.total = max(state.total, task.total_bytes)
        self._emit_progress()

    def _on_progress(self, state: _State, received: int, total: int):
        state.received = received
        if total > 0:
            state.total = max(state.total, total)
        self._emit_progress()

    def _on_exited(self, state: _State):
        if state not in self._active:
            return
        self._active.remove(state)
        job, part = state.job, state.job.dest + ".part"
        error = None
        if state.completed:
            try:
                os.replace(part, job.dest)
            except OSError as e:
                error = f"could not save the file: {e}"
        elif not self._cancelled:
            error = "; ".join(state.errors) or "download did not complete"
            if s3.is_s3(job.url) and not state.signed:
                error += _S3_HINT
        if error is None and state.completed:
            self._completed.append(job)
        elif error is not None:
            self._failed.append((job.name, error))
        if not state.completed:
            _remove_when_free(part)
        self._emit_progress()
        self._pump()

    def _emit_progress(self):
        done = len(self._completed) + len(self._failed)
        total_files = len(self._all)
        known_total = sum(s.total for s in self._all)
        if known_total and all(s.total for s in self._all):
            received = sum(s.total if s.job in self._completed else s.received for s in self._all)
            percent = int(100 * received / known_total)
        else:
            active = sum((s.received / s.total) if s.total else 0 for s in self._active)
            percent = int(100 * (done + active) / total_files) if total_files else 100
        self.progress.emit(done, total_files, min(percent, 100))

    def _maybe_finish(self):
        if self._finished_emitted or self._active or (self._queue and not self._cancelled):
            return
        self._finished_emitted = True
        self.finished.emit(list(self._completed), list(self._failed), self._cancelled)
