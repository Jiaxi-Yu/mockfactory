"""Base classes to handle catalog of objects."""

import os
import logging
import functools

import numpy as np

from . import mpi, utils
from .mpi import CurrentMPIComm
from .utils import BaseClass


def _multiple_columns(column):
    return isinstance(column, (list,tuple))


def vectorize_columns(func):
    @functools.wraps(func)
    def wrapper(self, column, **kwargs):
        if not _multiple_columns(column):
            return func(self,column,**kwargs)
        toret = [func(self,col,**kwargs) for col in column]
        if all(t is None for t in toret): # in case not broadcast to all ranks
            return None
        return np.asarray(toret)
    return wrapper


def _get_shape(size, itemshape):
    # join size and itemshape to get total shape
    if np.ndim(itemshape) == 0:
        return (size, itemshape)
    return (size,) + tuple(itemshape)


def _dict_to_array(data, struct=True):
    """
    Return dict as numpy array.

    Parameters
    ----------
    data : dict
        Data dictionary of name: array.

    struct : bool, default=True
        Whether to return structured array, with columns accessible through e.g. ``array['Position']``.
        If ``False``, numpy will attempt to cast types of different columns.

    Returns
    -------
    array : array
    """
    array = [(name,data[name]) for name in data]
    if struct:
        array = np.empty(array[0][1].shape[0], dtype=[(name, col.dtype, col.shape[1:]) for name,col in array])
        for name in data: array[name] = data[name]
    else:
        array = np.array([col for _,col in array])
    return array


class BaseFile(BaseClass):
    """
    Base class to read/write a file from/to disk.
    File handlers should extend this class, by (at least) implementing :meth:`read`, :meth:`get` and :meth:`write`.
    """
    _want_array = None

    @CurrentMPIComm.enable
    def __init__(self, filename, attrs=None, mode='', mpicomm=None):
        """
        Initialize :class:`BaseFile`.

        Parameters
        ----------
        filename : string, list of strings
            File name(s).

        attrs : dict, default=None
            File attributes. Will be complemented by those read from disk.
            These will eventually be written to disk.

        mode : string, default=''
            If 'r', read file header (necessary for further reading of file columns).

        mpicomm : MPI communicator, default=None
            The current MPI communicator.
        """
        mode = mode.lower()
        allowed_modes = ['r', 'w', 'rw', '']
        if mode not in allowed_modes:
            raise ValueError('mode must be one of {}'.format(allowed_modes))
        if not isinstance(filename, (list, tuple)):
            filename = [filename]
        self.filenames = list(filename)
        self.attrs = attrs or {}
        self.mpicomm = mpicomm
        self.mpiroot = 0
        if 'r' in mode:
            self._read_header()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_traceback):
        pass

    def is_mpi_root(self):
        """Whether current rank is root."""
        return self.mpicomm.rank == self.mpiroot

    def _read_header(self):
        if self.is_mpi_root():
            basenames = ['size', 'columns', 'attrs']
            self.csizes = []; self.columns = None; names = None
            for filename in self.filenames:
                self.log_info('Loading {}.'.format(filename))
                di = self._read_file_header(filename)
                self.csizes.append(di['size'])
                if self.columns is None:
                    self.columns = list(di['columns'])
                elif not set(di['columns']).issubset(self.columns):
                    raise ValueError('{} does not contain columns {}'.format(filename, set(di['columns']) - set(self.columns)))
                self.attrs = {**di.get('attrs', {}), **self.attrs}
                if names is None:
                    names = [name for name in di if name not in basenames]
                for name in names: # typically extension name
                    setattr(self, name, di[name])
            state = {name: getattr(self, name) for name in ['csizes'] + basenames[1:] + names}
        self.__dict__.update(self.mpicomm.bcast(state if self.is_mpi_root() else None, root=self.mpiroot))
        #self.mpicomm.Barrier() # necessary to avoid blocking due to file not found
        self.csize = sum(self.csizes)
        self.start = self.mpicomm.rank * self.csize // self.mpicomm.size
        self.stop = (self.mpicomm.rank + 1) * self.csize // self.mpicomm.size
        self.size = self.stop - self.start

    def read(self, column):
        """Read column of name ``column``."""
        if not hasattr(self, 'csizes'):
            self._read_header()
        cumsizes = np.cumsum(self.csizes)
        ifile_start = np.searchsorted(cumsizes, self.start, side='left') # cumsizes[i-1] < self.start <= cumsizes[i]
        ifile_stop = np.searchsorted(cumsizes, self.stop, side='left')
        toret = []
        for ifile in range(ifile_start, ifile_stop+1):
            cumstart = 0 if ifile == 0 else cumsizes[ifile - 1]
            rows = slice(max(self.start - cumstart, 0), min(self.stop - cumstart, self.csizes[ifile]))
            toret.append(self._read_file_slice(self.filenames[ifile], column, rows=rows))
        return np.concatenate(toret, axis=0)

    def write(self, data, mpiroot=None):
        """
        Write input data to file(s).

        Parameters
        ----------
        data : array, dict
            Data to write.

        mpiroot : int, default=None
            If ``None``, input array is assumed to be scattered across all ranks.
            Else the MPI rank where input array is gathered.
        """
        isdict = None
        if self.mpicomm.rank == mpiroot or mpiroot is None:
            isdict = isinstance(data, dict)
        if mpiroot is not None:
            isdict = self.mpicomm.bcast(isdict, root=mpiroot)
            if isdict:
                columns = self.mpicomm.bcast(list(data.keys()) if self.mpicomm.rank == mpiroot else None, root=mpiroot)
                data = {name: mpi.scatter_array(data[name] if self.mpicomm.rank == mpiroot else None, mpicomm=self.mpicomm, root=self.mpiroot) for name in colums}
            else:
                data = mpi.scatter_array(data, mpicomm=self.mpicomm, root=self.mpiroot)
        if isdict:
            for name in data: size = len(data[name]); break
        else:
            size = len(data)
        sizes = self.mpicomm.allgather(size)
        cumsizes = np.cumsum(sizes)
        csize = cumsizes[-1]
        nfiles = len(self.filenames)
        mpicomm = self.mpicomm # store current communicator
        for ifile, filename in enumerate(self.filenames):
            if self.is_mpi_root():
                self.log_info('Saving to {}.'.format(filename))
                utils.mkdir(os.path.dirname(filename))
        for ifile, filename in enumerate(self.filenames):
            start, stop = ifile * csize // nfiles, (ifile + 1) * csize // nfiles
            irank_start = np.searchsorted(cumsizes, start, side='left') # cumsizes[i-1] < self.start <= cumsizes[i]
            irank_stop = np.searchsorted(cumsizes, stop, side='left')
            rows = slice(0, 0)
            has_rows = irank_start <= self.mpicomm.rank <= irank_stop
            if irank_start <= self.mpicomm.rank <= irank_stop:
                cumstart = 0 if mpicomm.rank == 0 else cumsizes[mpicomm.rank - 1]
                rows = slice(max(start - cumstart, 0), min(stop - cumstart, sizes[mpicomm.rank]))
            #self.mpicomm = mpicomm.Split(has_rows, 0)
            #if not has_rows: continue
            if isdict:
                tmp = {name: data[name][rows] for name in data}
                if self._want_array:
                    tmp = _dict_to_array(tmp)
            else:
                tmp = data[sl]
                if not self._want_array:
                    tmp = {name: tmp[name] for name in tmp.dtype.names}
            self._write_file_slice(filename, tmp)
        self.mpicomm = mpicomm

    def _read_file_header(self, filename):
        """Return a dictionary of 'size', 'columns' at least for input ``filename``."""
        raise NotImplementedError('Implement method "_read_file" in your "{}"-inherited file handler'.format(self.__class__.___name__))

    def _read_file_slice(self, filename, column, rows):
        """
        Read rows ``rows`` of column ``column`` from file ``filename``.
        To be implemented in your file handler.
        """
        raise NotImplementedError('Implement method "_read_file_slice" in your "{}"-inherited file handler'.format(self.__class__.___name__))

    def _write_file_slice(self, filename, data):
        """
        Write ``data`` (``np.ndarray`` or ``dict``) to file ``filename``.
        To be implemented in your file handler.
        """
        raise NotImplementedError('Implement method "_write_file_slice" in your "{}"-inherited file handler'.format(self.__class__.___name__))


try: import fitsio
except ImportError: fitsio = None


class FitsFile(BaseFile):
    """
    Class to read/write a FITS file from/to disk.

    Note
    ----
    In some circumstances (e.g. catalog has just been written), :meth:`get` fails with a file not found error.
    We have tried making sure processes read the file one after the other, but that does not solve the issue.
    A similar issue happens with nbodykit - though at a lower frequency.
    """
    _extensions = ['fits']
    _want_array = True

    def __init__(self, filename, ext=None, **kwargs):
        """
        Initialize :class:`FitsFile`.

        Parameters
        ----------
        filename : string
            File name.

        ext : int, default=None
            FITS extension. Defaults to first extension with data.

        kwargs : dict
            Arguments for :class:`BaseFile`.
        """
        if fitsio is None:
            raise ImportError('Install fitsio')
        self.ext = ext
        super(FitsFile, self).__init__(filename=filename, **kwargs)

    def _read_file_header(self, filename):
        # Taken from https://github.com/bccp/nbodykit/blob/master/nbodykit/io/fits.py
        with fitsio.FITS(filename) as file:
            if getattr(self, 'ext') is None:
                for i, hdu in enumerate(file):
                    if hdu.has_data():
                        self.ext = i
                        break
                if self.ext is None:
                    raise IOError('{} has no binary table to read'.format(filename))
            else:
                if isinstance(self.ext, str):
                    if self.ext not in file:
                        raise IOError('{} does not contain extension with name {}'.format(filename, self.ext))
                elif self.ext >= len(file):
                    raise IOError('{} extension {} is not valid'.format(filename, self.ext))
            file = file[self.ext]
            # make sure we crash if data is wrong or missing
            if not file.has_data() or file.get_exttype() == 'IMAGE_HDU':
                raise IOError('{} extension {} is not a readable binary table'.format(filename, self.ext))
            return {'size': file.get_nrows(), 'columns':file.get_rec_dtype()[0].names, 'attrs': dict(file.read_header()), 'ext':self.ext}

    def _read_file_slice(self, filename, column, rows):
        return fitsio.read(filename, ext=self.ext, columns=column, rows=range(rows.start, rows.stop))
        #self.mpicomm.Barrier() # necessary to avoid blocking due to file not found
        #if not self.is_mpi_root():
        #    do = self.mpicomm.recv(source=self.mpicomm.rank-1, tag=42)
        #toret = fitsio.read(self.filename, ext=self.ext, columns=column, rows=range(self.start,self.stop))
        #if self.mpicomm.rank < self.mpicomm.size -1:
        #    self.mpicomm.send(True, dest=self.mpicomm.rank+1, tag=42)
        #return toret

    def _write_file_slice(self, filename, data):
        data = mpi.gather_array(data, mpicomm=self.mpicomm, root=self.mpiroot)
        if self.is_mpi_root():
            fitsio.write(filename, data, header=self.attrs.get('fitshdr',None), clobber=True)


try: import h5py
except ImportError: h5py = None


class HDF5File(BaseFile):
    """
    Class to read/write a HDF5 file from/to disk.

    Note
    ----
    In some circumstances (e.g. catalog has just been written), :meth:`get` fails with a file not found error.
    We have tried making sure processes read the file one after the other, but that does not solve the issue.
    A similar issue happens with nbodykit - though at a lower frequency.
    """
    _extensions = ['hdf', 'h4', 'hdf4', 'he2', 'h5', 'hdf5', 'he5', 'h5py']
    _want_array = False

    def __init__(self, filename, group='/', **kwargs):
        """
        Initialize :class:`HDF5File`.

        Parameters
        ----------
        filename : string
            File name.

        group : string, default='/'
            HDF5 group where columns are located.

        kwargs : dict
            Arguments for :class:`BaseFile`.
        """
        if h5py is None:
            raise ImportError('Install h5py')
        self.group = group
        if not group or group == '/'*len(group):
            self.group = '/'
        super(HDF5File, self).__init__(filename=filename, **kwargs)

    def _read_file_header(self, filename):
        with h5py.File(filename, 'r') as file:
            grp = file[self.group]
            columns = list(grp.keys())
            size = grp[columns[0]].shape[0]
            for name in columns:
                if grp[name].shape[0] != size:
                    raise IOError('Column {} has different length (expected {:d}, found {:d})'.format(name, size, grp[name].shape[0]))
            return {'size': size, 'columns':columns, 'attrs': dict(grp.attrs)}

    def _read_file_slice(self, filename, column, rows):
        #self.mpicomm.Barrier() # necessary to avoid blocking due to file not found
        with h5py.File(filename, 'r') as file:
            grp = file[self.group]
            return grp[column][rows]

    def _write_file_slice(self, filename, data):
        for name in data: size = len(data[name]); break
        driver = 'mpio'
        kwargs = {'comm': self.mpicomm}
        import h5py
        try:
            h5py.File(filename, 'w', driver=driver, **kwargs)
        except ValueError:
            driver = None
            kwargs = {}
        if driver == 'mpio':
            with h5py.File(filename, 'w', driver=driver, **kwargs) as file:
                cumsizes = np.cumsum([0] + self.mpicomm.allgather(size))
                start, stop = cumsizes[self.mpicomm.rank], cumsizes[self.mpicomm.rank+1]
                csize = cumsizes[-1]
                grp = file
                if self.group != '/':
                    grp = file.create_group(self.group)
                grp.attrs.update(self.attrs)
                for name in data:
                    dset = grp.create_dataset(name, shape=(csize,)+data[name].shape[1:], dtype=data[name].dtype)
                    dset[start:stop] = data[name]
        else:
            first = True
            for name in data:
                array = mpi.gather_array(data[name], mpicomm=self.mpicomm, root=self.mpiroot)
                if self.is_mpi_root():
                    with h5py.File(filename, 'w', driver=driver, **kwargs) as file:
                        grp = file
                        if first:
                            if self.group != '/':
                                grp = file.create_group(self.group)
                            grp.attrs.update(self.attrs)
                        dset = grp.create_dataset(name, data=array)
                first = False


class BinaryFile(BaseFile):
    """
    Class to read/write a binary file from/to disk.
    """
    _extensions = ['npy', 'bin']
    _want_array = True

    def _read_file_header(self, filename):
        array = np.load(filename, mmap_mode='r', allow_pickle=False, fix_imports=False)
        return {'size': len(array), 'columns':array.dtype.names, 'attrs': {}}

    def _read_file_slice(self, filename, column, rows):
        #self.mpicomm.Barrier() # necessary to avoid blocking due to file not found
        return np.load(filename, mmap_mode='r', allow_pickle=False, fix_imports=False)[column][rows]

    def _write_file_slice(self, filename, data):
        data = mpi.gather_array(data, mpicomm=self.mpicomm, root=self.mpiroot)
        if self.is_mpi_root():
            np.save(filename, data)
        # Maybe what is below actually works...
        #cumsizes = np.cumsum([0] + self.mpicomm.allgather(len(data)))
        #start, stop = cumsizes[self.mpicomm.rank], cumsizes[self.mpicomm.rank+1]
        #fp = np.memmap(filename, dtype=data.dtype, mode='w+', shape=cumsizes[-1])
        #fp[start:stop] = data
        #fp.flush()


class BaseCatalog(BaseClass):

    _attrs = ['attrs']

    """Base class that represents a catalog, as a dictionary of columns stored as arrays."""

    @CurrentMPIComm.enable
    def __init__(self, data=None, columns=None, attrs=None, mpicomm=None):
        """
        Initialize :class:`BaseCatalog`.

        Parameters
        ----------
        data : dict, BaseCatalog
            Dictionary of {name: array}.

        columns : list, default=None
            List of column names.
            Defaults to ``data.keys()``.

        attrs : dict, default=None
            Dictionary of other attributes.

        mpicomm : MPI communicator, default=None
            The current MPI communicator.
        """
        self.data = {}
        if columns is None:
            columns = list((data or {}).keys())
        if data is not None:
            for name in columns:
                self[name] = data[name]
        self.attrs = attrs or {}
        self.mpicomm = mpicomm
        self.mpiroot = 0

    def is_mpi_root(self):
        """Whether current rank is root."""
        return self.mpicomm.rank == self.mpiroot

    @classmethod
    def from_nbodykit(cls, catalog, columns=None):
        """
        Build new catalog from **nbodykit**.

        Parameters
        ----------
        catalog : nbodykit.base.catalog.CatalogSource
            **nbodykit** catalog.

        columns : list, default=None
            Columns to import. Defaults to all columns.

        Returns
        -------
        catalog : BaseCatalog
        """
        if columns is None: columns = catalog.columns
        data = {col: catalog[col].compute() for col in columns}
        return cls(data, mpicomm=catalog.comm, attrs=catalog.attrs)

    def to_nbodykit(self, columns=None):
        """
        Return catalog in **nbodykit** format.

        Parameters
        ----------
        columns : list, default=None
            Columns to export. Defaults to all columns.

        Returns
        -------
        catalog : nbodykit.source.catalog.ArrayCatalog
        """
        if columns is None: columns = self.columns()
        source = {col:self[col] for col in columns}
        from nbodykit.lab import ArrayCatalog
        attrs = {key:value for key,value in self.attrs.items() if key != 'fitshdr'}
        return ArrayCatalog(source, **attrs)

    def __len__(self):
        """Return catalog (local) length (``0`` if no column)."""
        keys = list(self.data.keys())
        if not keys:
            if self.has_source is not None:
                return self._source.size
            return 0
        return len(self[keys[0]])

    @property
    def size(self):
        """Equivalent for :meth:`__len__`."""
        return len(self)

    @property
    def csize(self):
        """Return catalog global size, i.e. sum of size in each process."""
        return self.mpicomm.allreduce(len(self))

    def columns(self, include=None, exclude=None):
        """
        Return catalog column names, after optional selections.

        Parameters
        ----------
        include : list, string, default=None
            Single or list of *regex* patterns to select column names to include.
            Defaults to all columns.

        exclude : list, string, default=None
            Single or list of *regex* patterns to select column names to exclude.
            Defaults to no columns.

        Returns
        -------
        columns : list
            Return catalog column names, after optional selections.
        """
        toret = None

        if self.is_mpi_root():
            allcols = set(self.data.keys())
            source = getattr(self, '_source', None)
            if source is not None:
                allcols |= set(source.columns)
            toret = allcols = list(allcols)

            def toregex(name):
                return name.replace('.','\.').replace('*','(.*)')

            if include is not None:
                if not isinstance(include,(tuple,list)):
                    include = [include]
                toret = []
                for inc in include:
                    inc = toregex(inc)
                    for col in allcols:
                        if re.match(inc,str(col)):
                            toret.append(col)
                allcols = toret

            if exclude is not None:
                if not isinstance(exclude,(tuple,list)):
                    exclude = [exclude]
                toret = []
                for exc in exclude:
                    exc = toregex(exc)
                    for col in allcols:
                        if re.match(exc,str(col)) is None:
                            toret.append(col)

        return self.mpicomm.bcast(toret,root=self.mpiroot)

    def __contains__(self, column):
        """Whether catalog contains column name ``column``."""
        return column in self.data

    def __iter__(self):
        """Iterate on catalog columns."""
        return iter(self.data)

    def cindices(self):
        """Row numbers in the global catalog."""
        sizes = self.mpicomm.allgather(len(self))
        sizes = [0] + np.cumsum(sizes[:1]).tolist()
        return sizes[self.mpicomm.rank] + np.arange(len(self))

    def zeros(self, itemshape=(), dtype='f8'):
        """Return array of size :attr:`size` filled with zero."""
        return np.zeros(_get_shape(len(self), itemshape), dtype=dtype)

    def ones(self, itemshape=(), dtype='f8'):
        """Return array of size :attr:`size` filled with one."""
        return np.ones(_get_shape(len(self), itemshape), dtype=dtype)

    def full(self, fill_value, itemshape=(), dtype='f8'):
        """Return array of size :attr:`size` filled with ``fill_value``."""
        return np.full(_get_shape(len(self), itemshape), fill_value, dtype=dtype)

    def falses(self, itemshape=()):
        """Return array of size :attr:`size` filled with ``False``."""
        return self.zeros(itemshape=itemshape, dtype=np.bool_)

    def trues(self, itemshape=()):
        """Return array of size :attr:`size` filled with ``True``."""
        return self.ones(itemshape=itemshape, dtype=np.bool_)

    def nans(self, itemshape=()):
        """Return array of size :attr:`size` filled with :attr:`numpy.nan`."""
        return self.ones(itemshape=itemshape)*np.nan

    @property
    def has_source(self):
        return getattr(self, '_source', None) is not None

    def get(self, column, *args, **kwargs):
        """Return catalog (local) column ``column`` if exists, else return provided default."""
        has_default = False
        if args:
            if len(args) > 1:
                raise SyntaxError('Too many arguments!')
            has_default = True
            default = args[0]
        if kwargs:
            if len(kwargs) > 1:
                raise SyntaxError('Too many arguments!')
            has_default = True
            default = kwargs['default']
        if column in self.data:
            return self.data[column]
        # if not in data, try in _source
        if self.has_source and column in self._source.columns:
            self.data[column] = self._source.read(column)
            return self.data[column]
        if has_default:
            return default
        raise KeyError('Column {} does not exist'.format(column))

    def set(self, column, item):
        """Set column of name ``column``."""
        self.data[column] = item

    def cget(self, column, mpiroot=None):
        """
        Return on process rank ``root`` catalog global column ``column`` if exists, else return provided default.
        If ``mpiroot`` is ``None`` or ``Ellipsis`` return result on all processes.
        """
        if mpiroot is None: mpiroot = Ellipsis
        return mpi.gather_array(self[column], mpicomm=self.mpicomm, root=mpiroot)

    def cslice(self, *args):
        """
        Perform global slicing of catalog,
        e.g. ``catalog.cslice(0,100,1)`` will return a new catalog of global size ``100``.
        Same reference to :attr:`attrs`.
        """
        sl = slice(*args)
        new = self.copy()
        for col in self.columns():
            self_value = self.cget(col, mpiroot=self.mpiroot)
            new[col] = mpi.scatter_array(self_value if self.is_mpi_root() else None, mpicomm=self.mpicomm, root=self.mpiroot)
        return new

    def to_array(self, columns=None, struct=True):
        """
        Return catalog as *numpy* array.

        Parameters
        ----------
        columns : list, default=None
            Columns to use. Defaults to all catalog columns.

        struct : bool, default=True
            Whether to return structured array, with columns accessible through e.g. ``array['Position']``.
            If ``False``, *numpy* will attempt to cast types of different columns.

        Returns
        -------
        array : array
        """
        if columns is None:
            columns = self.columns()
        data = {col: self[col] for col in columns}
        return _dict_to_array(data, struct=struct)

    @classmethod
    @CurrentMPIComm.enable
    def from_array(cls, array, columns=None, mpicomm=None, mpiroot=None, **kwargs):
        """
        Build :class:`BaseCatalog` from input ``array``.

        Parameters
        ----------
        array : array, dict
            Input array to turn into catalog.

        columns : list, default=None
            List of columns to read from array.
            If ``None``, inferred from ``array``.

        mpicomm : MPI communicator, default=None
            MPI communicator.

        mpiroot : int, default=None
            If ``None``, input array is assumed to be scattered across all ranks.
            Else the MPI rank where input array is gathered.

        kwargs : dict
            Other arguments for :meth:`__init__`.

        Returns
        -------
        catalog : BaseCatalog
        """
        isstruct = None
        if mpicomm.rank == mpiroot or mpiroot is None:
            isstruct = isdict = not hasattr(data, 'dtype')
            if isdict:
                if columns is None: columns = list(array.keys())
            else:
                isstruct = array.dtype.names is not None
                if isstruct and columns is None: columns = array.dtype.names
        if mpiroot is not None:
            isstruct = mpicomm.bcast(isstruct, root=mpiroot)
            columns = mpicomm.bcast(columns, root=mpiroot)
        columns = list(columns)
        new = cls(data=dict.fromkeys(columns), mpicomm=mpicomm, **kwargs)

        def get(column):
            value = None
            if mpicomm.rank == mpiroot or mpiroot is None:
                if isstruct:
                    value = array[column]
                else:
                    value = columns.index(column)
            if mpiroot is not None:
                return mpi.scatter_array(value, mpicomm=mpicomm, root=mpiroot)
            return value

        new.data = {column: get(column) for column in columns}
        return new

    def copy(self, columns=None):
        """Return copy, including column names ``columns`` (defaults to all columns)."""
        new = super(BaseCatalog,self).__copy__()
        if columns is None: columns = self.columns()
        new.data = {col: self[col] if col in self else None for col in columns}
        if new.has_source: new._source = self._source.copy()
        import copy
        for name in new._attrs:
            if hasattr(self, name):
                tmp = copy.copy(getattr(self, name))
                setattr(new, name, tmp)
        return new

    def deepcopy(self, columns=None):
        """Return copy, including column names ``columns`` (defaults to all columns)."""
        import copy
        new = self.copy(columns=columns)
        for name in self._attrs:
            if hasattr(self, name):
                setattr(new, name, copy.deepcopy(getattr(self, name)))
        new.data = {col:self[col].copy() for col in new}
        return new

    def __getstate__(self):
        """Return this class state dictionary."""
        data = {str(name): col for name, col in self.data.items()}
        state = {'data':data}
        for name in self._attrs:
            if hasattr(self, name):
                state[name] = getattr(self, name)
        return state

    def __setstate__(self, state):
        """Set the class state dictionary."""
        self.__dict__.update(state)

    @classmethod
    @CurrentMPIComm.enable
    def from_state(cls, state, mpicomm=None):
        """Create class from state."""
        new = cls.__new__(cls)
        new.__setstate__(state)
        new.mpicomm = mpicomm
        new.mpiroot = 0
        return new

    def __getitem__(self, name):
        """Get catalog column ``name`` if string, else return copy with local slice."""
        if isinstance(name, str):
            return self.get(name)
        new = self.copy()
        new.data = {col: self[col][name] for col in self.columns()}
        return new

    def __setitem__(self, name, item):
        """Set catalog column ``name`` if string, else set slice ``name`` of all columns to ``item``."""
        if isinstance(name,str):
            return self.set(name, item)
        for col in self.columns():
            self[col][name] = item

    def __delitem__(self, name):
        """Delete column ``name``."""
        try:
            del self.data[name]
        except KeyError as exc:
            if self.has_source is not None:
                self._source.columns.remove(name)
            else:
                raise KeyError('Column {} not found') from exc

    def __repr__(self):
        """Return string representation of catalog, including global size and columns."""
        return '{}(size={:d}, columns={})'.format(self.__class__.__name__, self.csize, self.columns())

    @classmethod
    def concatenate(cls, *others, keep_order=True):
        """
        Concatenate catalogs together.

        Parameters
        ----------
        others : list
            List of :class:`BaseCatalog` instances.

        keep_order : bool, default=False
            Whether to keep row order, which requires costly MPI-gather/scatter operations.
            If ``False``, rows on each MPI process will be added to those of the same MPI process.

        Returns
        -------
        new : BaseCatalog

        Warning
        -------
        :attr:`attrs` of returned catalog contains, for each key, the last value found in ``others`` :attr:`attrs` dictionaries.
        """
        if not others:
            raise ValueError('Provide at least one {} instance.'.format(cls.__name__))
        attrs = {}
        for other in others: attrs.update(other.attrs)
        others = [other for other in others if other.columns()]

        new = others[0].copy()
        new.attrs = attrs
        new_columns = new.columns()

        for other in others:
            other_columns = other.columns()
            assert new.mpicomm is other.mpicomm
            if new_columns and other_columns and set(other_columns) != set(new_columns):
                raise ValueError('Cannot extend samples as columns do not match: {} != {}.'.format(other_columns,new_columns))

        for column in new_columns:
            if keep_order:
                columns = [other.cget(column, mpiroot=new.mpiroot) for other in others]
                if new.is_mpi_root():
                    new[column] = np.concatenate(columns, axis=0)
                new[column] = mpi.scatter_array(new[column] if new.is_mpi_root() else None, root=new.mpiroot, mpicomm=new.mpicomm)
            else:
                new[column] = np.concatenate([other.get(column) for other in others], axis=0)
        return new

    def extend(self, other, **kwargs):
        """Extend catalog with ``other``."""
        new = self.concatenate(self, other, **kwargs)
        self.__dict__.update(new.__dict__)

    def __eq__(self, other):
        """Is ``self`` equal to ``other``, i.e. same type and columns? (ignoring :attr:`attrs`)"""
        if not isinstance(other,self.__class__):
            return False
        self_columns = self.columns()
        other_columns = other.columns()
        if set(other_columns) != set(self_columns):
            return False
        assert self.mpicomm == other.mpicomm
        toret = True
        for col in self_columns:
            self_value = self.cget(col, mpiroot=self.mpiroot)
            other_value = other.cget(col, mpiroot=self.mpiroot)
            if self.is_mpi_root():
                if not np.all(self_value == other_value):
                    toret = False
                    break
        return self.mpicomm.bcast(toret, root=self.mpiroot)

    @classmethod
    @CurrentMPIComm.enable
    def load_fits(cls, filename, ext=None,  mpicomm=None):
        """
        Load catalog in FITS binary format from disk.

        Parameters
        ----------
        filename : string
            File name to load catalog from.

        ext : int, default=None
            FITS extension. Defaults to first extension with data.

        mpicomm : MPI communicator, default=None
            The MPI communicator.

        Returns
        -------
        catalog : BaseCatalog
        """
        source = FitsFile(filename, ext=ext, mode='r', mpicomm=mpicomm)
        new = cls(attrs={'fitshdr': source.attrs}, mpicomm=mpicomm)
        new._source = source
        return new

    def save_fits(self, filename):
        """Save catalog to ``filename`` as *fits* file."""
        source = FitsFile(filename, ext=1, mpicomm=self.mpicomm)
        source.write({name: self[name] for name in self.columns()})

    @classmethod
    @CurrentMPIComm.enable
    def load_hdf5(cls, filename, group='/', mpicomm=None):
        """
        Load catalog in HDF5 binary format from disk.

        Parameters
        ----------
        filename : string
            File name to load catalog from.

        group : string, default='/'
            HDF5 group where columns are located.

        mpicomm : MPI communicator, default=None
            The MPI communicator.

        Returns
        -------
        catalog : BaseCatalog
        """
        source = HDF5File(filename, group=group, mode='r', mpicomm=mpicomm)
        new = cls(attrs=source.attrs, mpicomm=mpicomm)
        new._source = source
        return new

    def save_hdf5(self, filename, group='/'):
        """
        Save catalog to disk in *hdf5* binary format.

        Parameters
        ----------
        filename : string
            File name where to save catalog.

        group : string, default='/'
            HDF5 group where columns are located.
        """
        source = HDF5File(filename, group=group, mpicomm=self.mpicomm)
        source.write({name: self[name] for name in self.columns()})

    @classmethod
    @CurrentMPIComm.enable
    def load_binary(cls, filename, mpicomm=None):
        """
        Load catalog in *npy* binary format from disk.

        Parameters
        ----------
        columns : list, default=None
            List of column names to read. Defaults to all columns.

        mpicomm : MPI communicator, default=None
            The MPI communicator.

        Returns
        -------
        catalog : BaseCatalog
        """
        source = BinaryFile(filename, mode='r', mpicomm=mpicomm)
        new = cls(attrs=source.attrs, mpicomm=mpicomm)
        new._source = source
        return new

    def save_binary(self, filename):
        """
        Save catalog to disk in *npy* binary format.

        Parameters
        ----------
        filename : string
            File name where to save catalog.
        """
        source = BinaryFile(filename, mpicomm=self.mpicomm)
        source.write({name: self[name] for name in self.columns()})

    @classmethod
    @CurrentMPIComm.enable
    def load(cls, filename, mpicomm=None):
        """
        Load catalog in *npy* binary format from disk.

        Parameters
        ----------
        mpicomm : MPI communicator, default=None
            The MPI communicator.

        Returns
        -------
        catalog : BaseCatalog
        """
        mpiroot = 0
        if mpicomm.rank == mpiroot:
            cls.log_info('Loading {}.'.format(filename))
            state = np.load(filename, allow_pickle=True)[()]
            data = state.pop('data')
            columns = list(data.keys())
        else:
            state = None
            columns = None
        state = mpicomm.bcast(state, root=mpiroot)
        columns = mpicomm.bcast(columns, root=mpiroot)
        state['data'] = {}
        for name in columns:
            state['data'][name] = mpi.scatter_array(data[name] if mpicomm.rank == mpiroot else None, mpicomm=mpicomm, root=mpiroot)
        return cls.from_state(state, mpicomm=mpicomm)

    def save(self, filename):
        """Save catalog to ``filename`` as *npy* file."""
        if self.is_mpi_root():
            self.log_info('Saving to {}.'.format(filename))
            utils.mkdir(os.path.dirname(filename))
        state = self.__getstate__()
        state['data'] = {name: self.cget(name, mpiroot=self.mpiroot) for name in self.columns()}
        if self.is_mpi_root():
            np.save(filename, state, allow_pickle=True)

    @vectorize_columns
    def csum(self, column, axis=0):
        """Return global sum of column(s) ``column``."""
        return mpi.sum_array(self[column],axis=axis,mpicomm=self.mpicomm)

    @vectorize_columns
    def caverage(self, column, weights=None, axis=0):
        """Return global average of column(s) ``column``, with weights ``weights`` (defaults to ``1``)."""
        return mpi.average_array(self[column],weights=weights,axis=axis,mpicomm=self.mpicomm)

    @vectorize_columns
    def cmean(self, column, axis=0):
        """Return global mean of column(s) ``column``."""
        return self.caverage(column,axis=axis)

    @vectorize_columns
    def cmin(self, column, axis=0):
        """Return global minimum of column(s) ``column``."""
        return mpi.min_array(self[column],axis=axis,mpicomm=self.mpicomm)

    @vectorize_columns
    def cmax(self, column, axis=0):
        """Return global maximum of column(s) ``column``."""
        return mpi.max_array(self[column],axis=axis,mpicomm=self.mpicomm)


class Catalog(BaseCatalog):

    """A simple catalog."""
