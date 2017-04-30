import logging
import sys
import inspect
import itertools
import typing
from collections import OrderedDict

from katagawa.exc import NoSuchColumnError
from katagawa import kg as md_kg
from katagawa.orm.schema import column as md_column
from katagawa.orm.schema import row as md_row

PY36 = sys.version_info[0:2] >= (3, 6)
logger = logging.getLogger(__name__)


class TableMetadata(object):
    """
    The root class for table metadata.  
    This stores a registry of tables, and is responsible for calculating relationships etc.
     
    .. code-block:: python
    
        meta = TableMetadata()
        Table = table_base(metadata=meta)
    """

    def __init__(self):
        #: A registry of table name -> table object for this metadata.
        self.tables = {}

        #: The DB object bound to this metadata.
        self.bind = None  # type: md_kg.Katagawa

    def register_table(self, tbl: 'TableMeta') -> 'TableMeta':
        """
        Registers a new table object.
        
        :param tbl: The table to register. 
        """
        tbl._metadata = self
        self.tables[tbl.__tablename__] = tbl

        return tbl


class TableMeta(type):
    def __prepare__(*args, **kwargs):
        return OrderedDict()

    def __new__(mcs, n, b, c, register: bool = True):
        # hijack columns
        columns = OrderedDict()
        for col_name, value in c.copy().items():
            if isinstance(value, md_column.Column):
                columns[col_name] = value
                # nuke the column
                c.pop(col_name)

        c["_columns"] = columns
        return type.__new__(mcs, n, b, c)

    def __init__(self, tblname: str, tblbases: tuple, class_body: dict, register: bool = True):
        """
        Creates a new Table instance. 
        """
        # create the new type object
        super().__init__(tblname, tblbases, class_body)

        # emulate `__set_name__` on Python 3.5
        # also, set names on columns unconditionally
        if not PY36:
            it = itertools.chain(class_body.items(), self._columns.items())
        else:
            it = self._columns.items()

        for name, value in it:
            if hasattr(value, "__set_name__"):
                value.__set_name__(self, name)

        if register is False:
            return

        # ================ #
        # TABLE ATTRIBUTES #
        # ================ #

        try:
            self.__tablename__
        except AttributeError:
            #: The name of this table.
            self.__tablename__ = tblname.lower()

        #: The :class:`.Katagawa` this table is bound to.
        self.__bind = None

        #: A dict of columns for this table.
        self._columns = self._columns

        #: The primary key for this table.
        #: This should be a :class:`.PrimaryKey`.
        self._primary_key = self._calculate_primary_key()

        logger.debug("Registered new table {}".format(tblname))
        self._metadata.register_table(self)

    def __call__(self, *args, **kwargs):
        return self._get_table_row(**kwargs)

    def __getattr__(self, item):
        try:
            return next(filter(lambda col: col.name == item, self.columns))
        except StopIteration:
            raise AttributeError(item) from None

    @property
    def __quoted_name__(self):
        return '"{}"'.format(self.__tablename__)

    @property
    def columns(self) -> 'typing.List[md_column.Column]':
        """
        :return: A list of :class:`.Column` this Table has. 
        """
        return list(self.iter_columns())

    def iter_columns(self) -> 'typing.Generator[md_column.Column, None, None]':
        """
        :return: A generator that yields :class:`.Column` objects for this table. 
        """
        for col in self._columns.values():
            yield col

    def _calculate_primary_key(self) -> typing.Union['PrimaryKey', None]:
        """
        Calculates the current primary key for a table, given all the columns.

        If no columns are marked as a primary key, the key will not be generated.
        """
        pk_cols = []
        for col in self.iter_columns():
            if col.primary_key is True:
                pk_cols.append(col)

        if pk_cols:
            pk = PrimaryKey(*pk_cols)
            pk.table = self
            logger.debug("Calculated new primary key {}".format(pk))
            return pk

        return None

    @property
    def primary_key(self) -> 'PrimaryKey':
        """
        :getter: The :class:`.PrimaryKey` for this table.
        :setter: A new :class:.PrimaryKey` for this table.

        .. note::
            A primary key will automatically be calculated from columns at define time, if any
            columns have ``primary_key`` set to True.
        """
        return self._primary_key

    @primary_key.setter
    def primary_key(self, key: 'PrimaryKey'):
        key.table = self
        self._primary_key = key

    def _get_table_row(self, **kwargs) -> 'md_row.TableRow':
        """
        Gets a :class:`.TableRow` that represents this table.
        """
        col_map = {col.name: col for col in self.columns}
        row = md_row.TableRow(tbl=self)

        # lol
        if self.__init__ != TableMeta.__init__ and self.__init__ != object.__init__:
            self.__init__(row, **kwargs)
        else:
            for name, val in kwargs.items():
                if name not in col_map:
                    raise NoSuchColumnError(name)

                col_map[name].type.on_set(row, col_map[name], val)

        return row


def table_base(name: str = "Table", meta: 'TableMetadata' = None):
    """
    Gets a new base object to use for OO-style tables.  
    This object is the parent of all tables created in the object-oriented style; it provides some 
    key configuration to the relationship calculator and the Katagawa object itself.
    
    To use this object, you call this function to create the new object, and subclass it in your 
    table classes:
    
    .. code-block:: python
        Table = table_base()
        
        class User(Table):
            ...
            
    Binding the base object to the database object is essential for querying:
    
    .. code-block:: python
        # ensure the table is bound to that database
        db.bind_tables(Table)
        
        # now we can do queries
        sess = db.get_session()
        user = await sess.select(User).where(User.id == 2).first()
    
    Each Table object is associated with a database interface, which it uses for special querying
    inside the object, such as :meth:`.Table.get`.
    
    .. code-block:: python
        class User(Table):
            id = Column(Integer, primary_key=True)
            ...
        
        db.bind_tables(Table)    
        # later on, in some worker code
        user = await User.get(1)
    
    :param name: The name of the new class to produce. By default, it is ``Table``.
    :param meta: The :class:`.TableMetadata` to use as metadata.
    :return: A new Table class that can be used for OO tables.
    """
    if meta is None:
        meta = TableMetadata()

    class Table(metaclass=TableMeta, register=False):
        _metadata = meta

    Table.__name__ = name
    return Table


class PrimaryKey(object):
    """
    Represents the primary key of a table.
    
    A primary key can be on any 1 to N columns in a table.
    
    .. code-block:: python
        class Something(Table):
            first_id = Column(Integer)
            second_id = Column(Integer)
            
        pkey = PrimaryKey(Something.first_id, Something.second_id)
        Something.primary_key = pkey
        
    Alternatively, the primary key can be automatically calculated by passing ``primary_key=True`` 
    to columns in their constructor:
    
    .. code-block:: python
        class Something(Table):
            id = Column(Integer, primary_key=True)
            
        print(Something.primary_key)
    """

    def __init__(self, *cols: 'md_column.Column'):
        #: A list of :class:`.Column` that this primary key encompasses.
        self.columns = list(cols)  # type: typing.List[md_column.Column]

        #: The table this primary key is bound to.
        self.table = None

    def __repr__(self):
        return "<PrimaryKey table='{}' columns='{}'>".format(self.table, self.columns)