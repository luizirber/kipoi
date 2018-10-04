from __future__ import absolute_import
from __future__ import print_function

import os
import abc

import kipoi  # for .config module
from kipoi.specs import DataLoaderDescription, example_kwargs, print_dl_kwargs
from .utils import load_module, cd, getargs
from .external.torch.data import DataLoader
from kipoi.data_utils import (numpy_collate, numpy_collate_concat, get_dataset_item,
                              DataloaderIterable, batch_gen, get_dataset_lens, iterable_cycle)
from tqdm import tqdm
import types

import logging
logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

#
PREPROC_IFILE_TYPES = ['DNA_regions']
PREPROC_IFILE_FORMATS = ['bed3']


class BaseDataLoader(object):
    __metaclass__ = abc.ABCMeta

    @abc.abstractmethod
    def batch_iter(self, **kwargs):
        raise NotImplementedError

    def batch_train_iter(self, cycle=True, **kwargs):
        """Returns samples directly useful for training the model:
        (x["inputs"],x["targets"])

        Args:
          cycle: when True, the returned iterator will run indefinitely go through the dataset
            Use True with `fit_generator` in Keras.
          **kwargs: Arguments passed to self.batch_iter(**kwargs)
        """
        if cycle:
            return ((x["inputs"], x["targets"])
                    for x in iterable_cycle(self._batch_iterable(**kwargs)))
        else:
            return ((x["inputs"], x["targets"]) for x in self.batch_iter(**kwargs))

    def batch_predict_iter(self, **kwargs):
        """Returns samples directly useful for prediction x["inputs"]

        Args:
          **kwargs: Arguments passed to self.batch_iter(**kwargs)
        """
        return (x["inputs"] for x in self.batch_iter(**kwargs))

    def load_all(self, **kwargs):
        """Loads and returns the whole dataset

        Arguments:
            **kwargs: passed to batch_iter()
        """
        return numpy_collate_concat([x for x in tqdm(self.batch_iter(**kwargs))])

# --------------------------------------------
# Different implementations

# Other options:
# - generator - sample, batch-based
#   - yield.
# - iterator, iterable - sample, batch-based
#   - __iter__()
#     - __next__()
# - full dataset
#   - everything numpy arrays with the same first axis length


class PreloadedDataset(BaseDataLoader):
    """Generated by supplying a function returning the full dataset.

    The full dataset is a nested (list/dict) python structure of numpy arrays
    with the same first axis dimension.
    """
    data_fn = None

    @classmethod
    def from_fn(cls, data_fn):
        """setup the class variable
        """
        cls.data_fn = staticmethod(data_fn)
        return cls

    @classmethod
    def from_data(cls, data):
        return cls.from_data_fn(lambda: data)()

    @classmethod
    def _get_data_fn(cls):
        assert cls.data_fn is not None
        return cls.data_fn

    def __init__(self, *args, **kwargs):
        self.data = self._get_data_fn()(*args, **kwargs)
        lens = get_dataset_lens(self.data, require_numpy=True)
        # check that all dimensions are the same
        assert len(set(lens)) == 1
        self.n = lens[0]

    def __len__(self):
        return self.n

    def __getitem__(self, index):
        return get_dataset_item(self.data, index)

    def _batch_iterable(self, batch_size=32, shuffle=False, drop_last=False, **kwargs):
        """See batch_iter docs

        Returns:
          iterable
        """
        dl = DataLoader(self, batch_size=batch_size,
                        collate_fn=numpy_collate,
                        shuffle=shuffle,
                        num_workers=0,
                        drop_last=drop_last)
        return dl

    def batch_iter(self, batch_size=32, shuffle=False, drop_last=False, **kwargs):
        """Return a batch-iterator

        Arguments:
            dataset (Dataset): dataset from which to load the data.
            batch_size (int, optional): how many samples per batch to load
                (default: 1).
            shuffle (bool, optional): set to ``True`` to have the data reshuffled
                at every epoch (default: False).
            drop_last (bool, optional): set to ``True`` to drop the last incomplete batch,
                if the dataset size is not divisible by the batch size. If False and
                the size of dataset is not divisible by the batch size, then the last batch
                will be smaller. (default: False)

        Returns:
            iterator
        """
        dl = self._batch_iterable(batch_size=batch_size,
                                  shuffle=shuffle,
                                  drop_last=drop_last,
                                  **kwargs)
        return iter(dl)

    def load_all(self, **kwargs):
        """Load the whole dataset into memory

        Arguments:
            **kwargs: ignored
        """
        return self.data


class Dataset(BaseDataLoader):
    """An abstract class representing a Dataset.

    All other datasets should subclass it. All subclasses should override
    ``__len__``, that provides the size of the dataset, and ``__getitem__``,
    supporting integer indexing in range from 0 to len(self) exclusive.
    """

    __metaclass__ = abc.ABCMeta

    @abc.abstractmethod
    def __getitem__(self, index):
        """Return one sample

        index: {0, ..., len(self)-1}
        """
        raise NotImplementedError

    @abc.abstractmethod
    def __len__(self):
        """Return the number of all samples
        """
        raise NotImplementedError

    def _batch_iterable(self, batch_size=32, shuffle=False, num_workers=0, drop_last=False, **kwargs):
        """Return a batch-iteratrable

        See batch_iter docs

        Returns:
            Iterable
        """
        dl = DataLoader(self,
                        batch_size=batch_size,
                        collate_fn=numpy_collate,
                        shuffle=shuffle,
                        num_workers=num_workers,
                        drop_last=drop_last,
                        **kwargs)
        return dl

    def batch_iter(self, batch_size=32, shuffle=False, num_workers=0, drop_last=False, **kwargs):
        """Return a batch-iterator

        Arguments:
            dataset (Dataset): dataset from which to load the data.
            batch_size (int, optional): how many samples per batch to load
                (default: 1).
            shuffle (bool, optional): set to ``True`` to have the data reshuffled
                at every epoch (default: False).
            num_workers (int, optional): how many subprocesses to use for data
                loading. 0 means that the data will be loaded in the main process
                (default: 0)
            drop_last (bool, optional): set to ``True`` to drop the last incomplete batch,
                if the dataset size is not divisible by the batch size. If False and
                the size of dataset is not divisible by the batch size, then the last batch
                will be smaller. (default: False)

        Returns:
            iterator
        """
        dl = self._batch_iterable(batch_size=batch_size,
                                  shuffle=shuffle,
                                  num_workers=num_workers,
                                  drop_last=drop_last,
                                  **kwargs)
        return iter(dl)

    def load_all(self, batch_size=32, **kwargs):
        """Load the whole dataset into memory
        Arguments:
            batch_size (int, optional): how many samples per batch to load
                (default: 1).
        """
        return numpy_collate_concat([x for x in tqdm(self.batch_iter(batch_size, **kwargs))])


class BatchDataset(BaseDataLoader):
    """An abstract class representing a BatchDataset.
    """

    __metaclass__ = abc.ABCMeta

    @abc.abstractmethod
    def __getitem__(self, index):
        """Return one batch
        """
        raise NotImplementedError

    @abc.abstractmethod
    def __len__(self):
        """Number of all batches
        """
        raise NotImplementedError

    def _batch_iterable(self, num_workers=0, **kwargs):
        """Return a batch-iteratorable

        See batch_iter for docs
        """
        dl = DataLoader(self, batch_size=1,
                        collate_fn=numpy_collate_concat,
                        shuffle=False,
                        num_workers=num_workers,
                        drop_last=False)
        return dl

    def batch_iter(self, num_workers=0, **kwargs):
        """Return a batch-iterator

        Arguments:
            dataset (Dataset): dataset from which to load the data.
            num_workers (int, optional): how many subprocesses to use for data
                loading. 0 means that the data will be loaded in the main process
                (default: 0)
        Returns:
            iterator
        """
        dl = self._batch_iterable(num_workers=num_workers, **kwargs)
        return iter(dl)


class SampleIterator(BaseDataLoader):
    __metaclass__ = abc.ABCMeta

    @abc.abstractmethod
    def __iter__(self):
        raise NotImplementedError

    # TODO - how to maintain compatibility with python2?
    @abc.abstractmethod
    def __next__(self):
        raise NotImplementedError

    next = __next__

    def batch_iter(self, batch_size=32, **kwargs):
        return batch_gen(iter(self), batch_size=batch_size)

    def _batch_iterable(self, batch_size=32, **kwargs):
        kwargs['batch_size'] = batch_size
        return DataloaderIterable(self, kwargs)


class BatchIterator(BaseDataLoader):

    @abc.abstractmethod
    def __iter__(self):
        raise NotImplementedError

    # TODO - how to maintain compatibility with python2?
    @abc.abstractmethod
    def __next__(self):
        raise NotImplementedError

    next = __next__

    def batch_iter(self, **kwargs):
        return iter(self)

    def _batch_iterable(self, **kwargs):
        return DataloaderIterable(self, kwargs)


class SampleGenerator(BaseDataLoader):
    """Transform a generator of samples into SampleIterator
    """
    generator_fn = None

    @classmethod
    def from_fn(cls, generator_fn):
        """setup the class variable
        """
        cls.generator_fn = staticmethod(generator_fn)
        return cls

    @classmethod
    def _get_generator_fn(cls):
        assert cls.generator_fn is not None
        return cls.generator_fn

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def __iter__(self):
        """Return a new generator every time
        """
        return self._get_generator_fn()(*self.args, **self.kwargs)

    def batch_iter(self, batch_size=32, **kwargs):
        return batch_gen(iter(self), batch_size=batch_size)

    def _batch_iterable(self, batch_size=32, **kwargs):
        kwargs['batch_size'] = batch_size
        return DataloaderIterable(self, kwargs)


class BatchGenerator(BaseDataLoader):
    """Transform a generator of batches into BatchIterator
    """
    generator_fn = None

    @classmethod
    def from_fn(cls, generator_fn):
        cls.generator_fn = staticmethod(generator_fn)
        return cls

    @classmethod
    def _get_generator_fn(cls):
        assert cls.generator_fn is not None
        return cls.generator_fn

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def __iter__(self):
        return self._get_generator_fn()(*self.args, **self.kwargs)

    def batch_iter(self, **kwargs):
        return iter(self)

    def _batch_iterable(self, **kwargs):
        return DataloaderIterable(self, kwargs)
# --------------------------------------------


def get_dataloader_factory(dataloader, source="kipoi"):
    """Loads the dataloader

    # Arguments
        dataloader (str): dataloader name
        source (str): source name

    # Returns
    - Instance of class inheriting from `kipoi.data.BaseDataLoader` (like `kipoi.data.Dataset`)
           decorated with additional attributes.

    # Methods
    - __batch_iter(batch_size, num_workers, **kwargs)__
         - Arguments
             - **batch_size**: batch size
             - **num_workers**: Number of workers to use in parallel.
             - ****kwargs**: Other kwargs specific to each dataloader
         - Yields
             - `dict` with `"inputs"`, `"targets"` and `"metadata"`
    - __batch_train_iter(cycle=True, **kwargs)__
         - Arguments
             - **cycle**: if True, cycle indefinitely
             - ****kwargs**: Kwargs passed to `batch_iter()` like `batch_size`
         - Yields
             - tuple of ("inputs", "targets") from the usual dict returned by `batch_iter()`
    - __batch_predict_iter(**kwargs)__
         - Arguments
             - ****kwargs**: Kwargs passed to `batch_iter()` like `batch_size`
         - Yields
             - "inputs" field from the usual dict returned by `batch_iter()`
    - __load_all(**kwargs)__ - load the whole dataset into memory
         - Arguments
             - ****kwargs**: Kwargs passed to `batch_iter()` like `batch_size`
         - Returns
             - `dict` with `"inputs"`, `"targets"` and `"metadata"`
    - **init_example()** - instantiate the dataloader with example kwargs
    - **print_args()** - print information about the required arguments

    # Appended attributes
    - **type** (str): dataloader type (class name)
    - **defined_as** (str): path and dataloader name
    - **args** (list of kipoi.specs.DataLoaderArgument): datalaoder argument description
    - **info** (kipoi.specs.Info): general information about the dataloader
    - **schema** (kipoi.specs.DataloaderSchema): information about the input/output
            data modalities
    - **dependencies** (kipoi.specs.Dependencies): class specifying the dependencies.
          (implements `install` method for running the installation)
    - **name** (str): model name
    - **source** (str): model source
    - **source_dir** (str): local path to model source storage
    - **postprocessing** (dict): dictionary of loaded plugin specifications
    - **example_kwargs** (dict): kwargs for running the provided example
    """

    # pull the dataloader & get the dataloader directory
    source = kipoi.config.get_source(source)
    yaml_path = source.pull_dataloader(dataloader)
    dataloader_dir = os.path.dirname(yaml_path)

    # --------------------------------------------
    # Setup dataloader description
    with cd(dataloader_dir):  # move to the dataloader directory temporarily
        dl = DataLoaderDescription.load(os.path.basename(yaml_path))
        file_path, obj_name = tuple(dl.defined_as.split("::"))
        CustomDataLoader = getattr(load_module(file_path), obj_name)

    # check that dl.type is correct
    if dl.type not in AVAILABLE_DATALOADERS:
        raise ValueError("dataloader type: {0} is not in supported dataloaders:{1}".
                         format(dl.type, list(AVAILABLE_DATALOADERS.keys())))
    # check that the extractor arguments match yaml arguments
    if not getargs(CustomDataLoader) == set(dl.args.keys()):
        raise ValueError("DataLoader arguments: \n{0}\n don't match ".format(set(getargs(CustomDataLoader))) +
                         "the specification in the dataloader.yaml file:\n{0}".
                         format(set(dl.args.keys())))
    # check that CustomDataLoader indeed interits from the right DataLoader
    if dl.type in DATALOADERS_AS_FUNCTIONS:
        # transform the functions into objects
        assert isinstance(CustomDataLoader, types.FunctionType)
        CustomDataLoader = AVAILABLE_DATALOADERS[dl.type].from_fn(CustomDataLoader)
    else:
        if not issubclass(CustomDataLoader, AVAILABLE_DATALOADERS[dl.type]):
            raise ValueError("DataLoader does't inherit from the specified dataloader: {0}".
                             format(AVAILABLE_DATALOADERS[dl.type].__name__))
    logger.info('successfully loaded the dataloader from {}'.
                format(os.path.normpath(os.path.join(dataloader_dir, dl.defined_as))))
    # Inherit the attributes from dl
    # TODO - make this more automatic / DRY
    # write a method to load those things?
    CustomDataLoader.type = dl.type
    CustomDataLoader.defined_as = dl.defined_as
    CustomDataLoader.args = dl.args
    CustomDataLoader.info = dl.info
    CustomDataLoader.output_schema = dl.output_schema
    CustomDataLoader.dependencies = dl.dependencies
    CustomDataLoader.postprocessing = dl.postprocessing
    # keep it hidden?
    CustomDataLoader._yaml_path = yaml_path
    CustomDataLoader.source = source
    # TODO - rename?
    CustomDataLoader.source_dir = dataloader_dir

    # Add init_example method.
    # example_kwargs also downloads files to {dataloader_dir}/dataloader_files
    CustomDataLoader.example_kwargs = example_kwargs(CustomDataLoader.args, dataloader_dir)

    def init_example(cls):
        return cls(**cls.example_kwargs)
    CustomDataLoader.init_example = classmethod(init_example)
    CustomDataLoader.print_args = classmethod(print_dl_kwargs)

    return CustomDataLoader


AVAILABLE_DATALOADERS = {"PreloadedDataset": PreloadedDataset,
                         "Dataset": Dataset,
                         "BatchDataset": BatchDataset,
                         "SampleIterator": SampleIterator,
                         "SampleGenerator": SampleGenerator,
                         "BatchIterator": BatchIterator,
                         "BatchGenerator": BatchGenerator}

DATALOADERS_AS_FUNCTIONS = ["PreloadedDataset", "SampleGenerator", "BatchGenerator"]
