#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Wed Aug 29 14:31:52 2018

@author: scp-173
"""


"""Dataset generator."""
__all__ = ['DataLoader']

import pickle
import io
import sys
import multiprocessing
import multiprocessing.queues
from multiprocessing.reduction import ForkingPickler
import threading
import numpy as np

import sampler as _sampler

class ConnectionWrapper(object):
    """Connection wrapper for multiprocessing that supports sending
    NDArray via shared memory."""

    def __init__(self, conn):
        self._conn = conn

    def send(self, obj):
        """Send object"""
        buf = io.BytesIO()
        ForkingPickler(buf, pickle.HIGHEST_PROTOCOL).dump(obj)
        self.send_bytes(buf.getvalue())

    def recv(self):
        """Receive object"""
        buf = self.recv_bytes()
        return pickle.loads(buf)

    def __getattr__(self, name):
        """Emmulate conn"""
        attr = self.__dict__.get('_conn', None)
        return getattr(attr, name)


class Queue(multiprocessing.queues.Queue):
    """Wrapper for multiprocessing queue that dumps NDArray with shared memory."""
    def __init__(self, *args, **kwargs):
        if sys.version_info[0] <= 2:
            super(Queue, self).__init__(*args, **kwargs)
        else:
            super(Queue, self).__init__(*args, ctx=multiprocessing.get_context(),
                                        **kwargs)
        self._reader = ConnectionWrapper(self._reader)
        self._writer = ConnectionWrapper(self._writer)
        self._send = self._writer.send
        self._recv = self._reader.recv


class SimpleQueue(multiprocessing.queues.SimpleQueue):
    """Wrapper for multiprocessing SimpleQueue that dumps NDArray with shared memory.
       SimpleQueue don't use threading internally.
    """
    def __init__(self, *args, **kwargs):
        if sys.version_info[0] <= 2:
            super(SimpleQueue, self).__init__(*args, **kwargs)
        else:
            super(SimpleQueue, self).__init__(*args, ctx=multiprocessing.get_context(),
                                              **kwargs)
        self._reader = ConnectionWrapper(self._reader)
        self._writer = ConnectionWrapper(self._writer)
        self._send = self._writer.send
        self._recv = self._reader.recv


def default_batchify_fn(data):
    """Collate data into batch."""
    if isinstance(data[0], np.ndarray):
        #print(data[0].shape)
        return np.stack(data)
    elif isinstance(data[0], tuple):
        data = zip(*data)
        return [default_batchify_fn(i) for i in data]
    else:
        data = np.asarray(data)
        return np.array(data, dtype=data.dtype)


def default_mp_batchify_fn(data):
    """Collate data into batch. Use shared memory for stacking."""
    if isinstance(data[0], np.ndarray):
        out = np.empty((len(data),) + data[0].shape, dtype=data[0].dtype)
        return np.stack(data, out=out)
    elif isinstance(data[0], tuple):
        data = zip(*data)
        return [default_mp_batchify_fn(i) for i in data]
    else:
        data = np.asarray(data)
        return np.array(data, dtype=data.dtype)


def worker_loop(dataset, key_queue, data_queue, batchify_fn):
    """Worker loop for multiprocessing DataLoader."""
    if hasattr(dataset, '_fork') and callable(dataset._fork):
        dataset._fork()
    while True:
        idx, samples = key_queue.get()
        if idx is None:
            break
        batch = batchify_fn([dataset[i] for i in samples])
        data_queue.put((idx, batch))

def fetcher_loop(data_queue, data_buffer):
    """Fetcher loop for fetching data from queue and put in reorder dict."""
    while True:
        idx, batch = data_queue.get()
        if idx is None:
            break
        data_buffer[idx] = batch

class _MultiWorkerIter(object):
    """Interal multi-worker iterator for DataLoader."""
    def __init__(self, num_workers, dataset, batchify_fn, batch_sampler,
                 worker_fn=worker_loop):
        assert num_workers > 0, "_MultiWorkerIter is not for {} workers".format(num_workers)
        self._num_workers = num_workers
        self._dataset = dataset
        self._batchify_fn = batchify_fn
        self._batch_sampler = batch_sampler
        self._key_queue = Queue()
        self._data_queue = Queue() if sys.version_info[0] <= 2 else SimpleQueue()
        self._data_buffer = {}
        self._rcvd_idx = 0
        self._sent_idx = 0
        self._iter = iter(self._batch_sampler)
        self._shutdown = False

        workers = []
        for _ in range(self._num_workers):
            worker = multiprocessing.Process(
                target=worker_fn,
                args=(self._dataset, self._key_queue, self._data_queue, self._batchify_fn))
            worker.daemon = True
            worker.start()
            workers.append(worker)

        self._fetcher = threading.Thread(
            target=fetcher_loop,
            args=(self._data_queue, self._data_buffer))
        self._fetcher.daemon = True
        self._fetcher.start()

        # pre-fetch
        for _ in range(2 * self._num_workers):
            self._push_next()

    def __len__(self):
        return len(self._batch_sampler)

    def __del__(self):
        self.shutdown()

    def _push_next(self):
        """Assign next batch workload to workers."""
        r = next(self._iter, None)
        if r is None:
            return
        self._key_queue.put((self._sent_idx, r))
        self._sent_idx += 1

    def __next__(self):
        assert not self._shutdown, "call __next__ after shutdown is forbidden"
        if self._rcvd_idx == self._sent_idx:
            assert not self._data_buffer, "Data buffer should be empty at this moment"
            self.shutdown()
            raise StopIteration

        while True:
            if self._rcvd_idx in self._data_buffer:
                batch = self._data_buffer.pop(self._rcvd_idx)
                self._rcvd_idx += 1
                self._push_next()
                return batch

    def next(self):
        return self.__next__()

    def __iter__(self):
        return self

    def shutdown(self):
        """Shutdown internal workers by pushing terminate signals."""
        if not self._shutdown:
            for _ in range(self._num_workers):
                self._key_queue.put((None, None))
            self._data_queue.put((None, None))
            self._shutdown = True


class DataLoader(object):
    """Loads data from a dataset and returns mini-batches of data.

    Parameters
    ----------
    dataset : Dataset
        Source dataset. Note that numpy and mxnet arrays can be directly used
        as a Dataset.
    batch_size : int
        Size of mini-batch.
    shuffle : bool
        Whether to shuffle the samples.
    sampler : Sampler
        The sampler to use. Either specify sampler or shuffle, not both.
    last_batch : {'keep', 'discard', 'rollover'}
        How to handle the last batch if batch_size does not evenly divide
        `len(dataset)`.

        keep - A batch with less samples than previous batches is returned.
        discard - The last batch is discarded if its incomplete.
        rollover - The remaining samples are rolled over to the next epoch.
    batch_sampler : Sampler
        A sampler that returns mini-batches. Do not specify batch_size,
        shuffle, sampler, and last_batch if batch_sampler is specified.
    batchify_fn : callable
        Callback function to allow users to specify how to merge samples
        into a batch. Defaults to `default_batchify_fn`::

            def default_batchify_fn(data):
                if isinstance(data[0], nd.NDArray):
                    return nd.stack(*data)
                elif isinstance(data[0], tuple):
                    data = zip(*data)
                    return [default_batchify_fn(i) for i in data]
                else:
                    data = np.asarray(data)
                    return nd.array(data, dtype=data.dtype)

    num_workers : int, default 0
        The number of multiprocessing workers to use for data preprocessing.
    pin_memory : boolean, default False
        If ``True``, the dataloader will copy NDArrays into pinned memory
        before returning them. Copying from CPU pinned memory to GPU is faster
        than from normal CPU memory.
    """
    def __init__(self, dataset, batch_size=None, shuffle=False, sampler=None,
                 last_batch=None, batch_sampler=None, batchify_fn=None,
                 num_workers=0):
        self._dataset = dataset

        if batch_sampler is None:
            if batch_size is None:
                raise ValueError("batch_size must be specified unless " \
                                 "batch_sampler is specified")
            if sampler is None:
                if shuffle:
                    sampler = _sampler.RandomSampler(len(dataset))
                else:
                    sampler = _sampler.SequentialSampler(len(dataset))
            elif shuffle:
                raise ValueError("shuffle must not be specified if sampler is specified")

            batch_sampler = _sampler.BatchSampler(
                sampler, batch_size, last_batch if last_batch else 'keep')
        elif batch_size is not None or shuffle or sampler is not None or \
                last_batch is not None:
            raise ValueError("batch_size, shuffle, sampler and last_batch must " \
                             "not be specified if batch_sampler is specified.")

        self._batch_sampler = batch_sampler
        self._num_workers = num_workers if num_workers >= 0 else 0
        if batchify_fn is None:
            if num_workers > 0:
                self._batchify_fn = default_mp_batchify_fn
            else:
                self._batchify_fn = default_batchify_fn
        else:
            self._batchify_fn = batchify_fn

    def __iter__(self):
        if self._num_workers == 0:
            def same_process_iter():
                for batch in self._batch_sampler:

                    ret = self._batchify_fn([self._dataset[idx] for idx in batch])
                    yield ret
            return same_process_iter()

        # multi-worker
        return _MultiWorkerIter(self._num_workers, self._dataset,
                                self._batchify_fn, self._batch_sampler)

    def __len__(self):
        return len(self._batch_sampler)


if __name__ == '__main__':
    from image_dataset import ImageFolderDataset
    from transform_tmp import transform
    dataset_path = '/data/dogs_vs_cats/train'
    dataset = ImageFolderDataset(dataset_path, transform=transform)
    loader = DataLoader(dataset, batch_size=128, num_workers=8, shuffle=True)
    
    import time
    a = time.time()
    
    test = 0
    for data, label in loader:
        print(test)
        print(data.shape, label)
        test += 1
    print(time.time() - a )
    
        
    
    import matplotlib.pyplot as plt
    plt.imshow(data[1, :, :, :])
    plt.show()