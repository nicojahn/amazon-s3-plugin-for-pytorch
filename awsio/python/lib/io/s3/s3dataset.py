#   Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
  
#   Licensed under the Apache License, Version 2.0 (the "License").
#   You may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
  
#       http://www.apache.org/licenses/LICENSE-2.0
  
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

import tarfile
import io
import zipfile
import re
from torch.utils.data import IterableDataset, Dataset
import torch
import torch.distributed as dist
import _pywrap_s3_io
import random
from itertools import chain

meta_prefix = "__"
meta_suffix = "__"

def reraise_exception(exn): # pragma: no cover
    """Called in an exception handler to re-raise the exception."""
    raise exn


def tardata(fileobj, skip_meta=r"__[^/]*__($|/)", handler=reraise_exception):
    """Iterator yielding filename, content pairs for the given tar stream.
    """
    # eliminated from test coverage since checking requires invalid tarfile
    try:
        stream = tarfile.open(fileobj=io.BytesIO(fileobj), mode="r|*")
        for tarinfo in stream:
            try:
                if not tarinfo.isreg(): # pragma: no cover
                    continue
                fname = tarinfo.name
                if fname is None: # pragma: no cover
                    continue
                if ("/" not in fname and fname.startswith(meta_prefix)
                        and fname.endswith(meta_suffix)): # pragma: no cover
                    # skipping metadata for now
                    continue
                if skip_meta is not None and re.match(skip_meta, fname): # pragma: no cover
                    continue
                data = stream.extractfile(tarinfo).read()
                yield fname, data
            except Exception as exn: # pragma: no cover
                if handler(exn):
                    continue
                else:
                    break
        del stream
    except Exception as exn: # pragma: no cover
        handler(exn)


def zipdata(fileobj, handler=reraise_exception):
    """Iterator yielding filename, content pairs for the given zip stream.
    """
    # eliminated from test coverage since checking requires invalid zipfile
    try:
        with zipfile.ZipFile(io.BytesIO(fileobj), 'r') as zfile:
            try:
                for file_ in zfile.namelist():
                    data = zfile.read(file_)
                    yield file_, data
            except Exception as exn: # pragma: no cover
                print("Error:", exn)
    except Exception as exn: # pragma: no cover
        print("Error:", exn)


def file_exists(url):
    """Return if file exists or not"""
    handler = _pywrap_s3_io.S3Init()
    return handler.file_exists(url)


def get_file_size(url):
    """Return the file size of the specified file"""
    handler = _pywrap_s3_io.S3Init()
    return handler.get_file_size(url)


def list_files(url):
    """Returns a list of entries under the same prefix.
    """
    handler = _pywrap_s3_io.S3Init()
    return handler.list_files(url)


class S3BaseClass(object):
    """A base class for defining urls_list for S3Dataset and S3IterableDataset
    """
    def __init__(self, urls_list):
        urls = [urls_list] if isinstance(urls_list, str) else urls_list
        self._urls_list = self.create_urls_list(urls)

    def create_urls_list(self, urls):
        handler = _pywrap_s3_io.S3Init()
        urls_list = list()
        for url in urls:
            if not handler.file_exists(url):
                url_objects = handler.list_files(url)
                assert len(url_objects) != 0, \
                    f"The directory {url} does not contain any objects."
                urls_list.extend(url_objects)
            elif urls_list:
                urls_list.append(url)
            else:
                urls_list = [url]
        return urls_list

    @property
    def urls_list(self):
        return self._urls_list


class S3Dataset(S3BaseClass, Dataset):
    """A mapped-style dataset for objects in s3.
    """
    def __init__(self, urls_list):
        """
        Args:
            urls_list (string or list of strings): the prefix(es) and
                filenames starting with 's3://'. Each string is assumed
                as a filename first. If the file doesn't exist, the string
                is assumed as a prefix.
        """
        S3BaseClass.__init__(self, urls_list)
        self.handler = _pywrap_s3_io.S3Init()

    def __len__(self):
        return len(self.urls_list)

    def __getitem__(self, idx):
        filename = self.urls_list[idx]
        fileobj = self.handler.s3_read(filename)
        return filename, fileobj


class S3IterableDataset(S3BaseClass, IterableDataset):
    """Iterate over s3 dataset.
    It handles some bookkeeping related to DataLoader.
    """
    def __init__(self, urls_list, shuffle_urls=False):
        self.epoch = 0
        self.shuffle_urls = shuffle_urls
        self.dist = dist.is_initialized() if dist.is_available() else False
        if self.dist:
            self.world_size = dist.get_world_size()
            self.rank = dist.get_rank()
        S3BaseClass.__init__(self, urls_list)

    @property
    def shuffled_list(self):
        if self.shuffle_urls:
            random.seed(self.epoch)
            return random.sample(self.urls_list, len(self.urls_list))
        else:
            return self.urls_list

    def download_data(self, filename):
        if filename[-3:] == "tar":
            tarfile = tardata(self.handler.s3_read(filename))
            for fname, content in tarfile:
                yield fname, content
        elif filename[-3:] == "zip":
            zipfile = zipdata(self.handler.s3_read(filename))
            for fname, content in zipfile:
                yield fname, content
        else:
            yield filename, self.handler.s3_read(filename)

    def get_stream(self, urls_list):
        return chain.from_iterable(map(self.download_data, urls_list))

    def worker_dist(self, urls):
        if self.dist:
            total_size = len(urls)
            urls = urls[self.rank:total_size:self.world_size]

        worker_info = torch.utils.data.get_worker_info()
        if worker_info is not None:
            wid = worker_info.id
            num_workers = worker_info.num_workers
            length = len(urls)
            return urls[wid:length:num_workers]
        else:
            return urls

    def __iter__(self):
        self.handler = _pywrap_s3_io.S3Init()
        urls = self.worker_dist(self.shuffled_list)
        return self.get_stream(urls)

    def __len__(self):
        return len(self.urls_list)

    def set_epoch(self, epoch):
        self.epoch = epoch


class ShuffleDataset(torch.utils.data.IterableDataset):
    def __init__(self, dataset, buffer_size):
        super().__init__()
        self.dataset = dataset
        self.buffer_size = buffer_size

    def __iter__(self):
        shufbuf = []
        try:
            dataset_iter = iter(self.dataset)
            for _ in range(self.buffer_size):
                shufbuf.append(next(dataset_iter))
        except StopIteration:
            self.buffer_size = len(shufbuf)

        try:
            while True:
                try:
                    if self.buffer_size == 0:
                        break
                    evict_idx = random.randint(0, self.buffer_size - 1)
                    yield shufbuf.pop(evict_idx)
                    item = next(dataset_iter)
                    shufbuf.append(item)
                except StopIteration:
                    break
            while len(shufbuf) > 0:
                evict_idx = random.randint(0, len(shufbuf) - 1)
                yield shufbuf.pop(evict_idx)
        except GeneratorExit: # pragma: no cover
            pass
