"""Use to transfer a MySQL database to SQLite."""

from __future__ import division

import logging
import re
import sqlite3
import sys
from math import ceil
from os.path import realpath

import mysql.connector
import six
from mysql.connector import errorcode  # pylint: disable=C0412
from slugify import slugify
from tqdm import trange

if six.PY2:
    from .sixeptions import *  # pylint: disable=W0622,W0401,W0614


class MySQLtoSQLite:  # pylint: disable=R0902,R0903
    """Use this class to transfer a MySQL database to SQLite."""

    COLUMN_PATTERN = re.compile(r"^[^(]+")
    COLUMN_LENGTH_PATTERN = re.compile(r"\(\d+\)$")

    def __init__(self, **kwargs):  # noqa: ignore=C901
        """Constructor."""
        self._mysql_user = kwargs.get("mysql_user") or None
        if not self._mysql_user:
            raise ValueError("Please provide a MySQL user")
        self._mysql_user = str(self._mysql_user)

        self._mysql_password = kwargs.get("mysql_password") or None
        if self._mysql_password:
            self._mysql_password = str(self._mysql_password)

        self._mysql_host = kwargs.get("mysql_host") or "localhost"
        if self._mysql_host:
            self._mysql_host = str(self._mysql_host)

        self._mysql_port = kwargs.get("mysql_port") or 3306
        if self._mysql_port:
            self._mysql_port = int(self._mysql_port)

        self._current_chunk_number = 0
        self._chunk_size = kwargs.get("chunk") or None
        if self._chunk_size:
            self._chunk_size = int(self._chunk_size)

        self._mysql_database = kwargs.get("mysql_database")
        if not self._mysql_database:
            raise ValueError("Please provide a MySQL database")

        self._sqlite_file = kwargs.get("sqlite_file") or None

        self._buffered = kwargs.get("buffered") or False

        self._vacuum = kwargs.get("vacuum") or False

        self._logger = self._setup_logger(log_file=kwargs.get("log_file") or None)

        self._sqlite = sqlite3.connect(realpath(self._sqlite_file))
        self._sqlite.row_factory = sqlite3.Row

        self._sqlite_cur = self._sqlite.cursor()

        try:
            self._mysql = mysql.connector.connect(
                user=self._mysql_user,
                password=self._mysql_password,
                host=self._mysql_host,
                port=self._mysql_port,
            )
            if not self._mysql.is_connected():
                raise ConnectionError("Unable to connect to MySQL")

            self._mysql_cur = self._mysql.cursor(raw=True, buffered=self._buffered)
            self._mysql_cur_dict = self._mysql.cursor(
                dictionary=True, buffered=self._buffered
            )
            try:
                self._mysql.database = self._mysql_database
            except (mysql.connector.Error, Exception) as err:  # pylint: disable=W0703
                if hasattr(err, "errno") and err.errno == errorcode.ER_BAD_DB_ERROR:
                    self._logger.error("MySQL Database does not exist!")
                    raise
                self._logger.error(err)
                raise
        except mysql.connector.Error as err:
            self._logger.error(err)
            raise

    @classmethod
    def _setup_logger(cls, log_file=None):
        formatter = logging.Formatter(
            fmt="%(asctime)s %(levelname)-8s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        )
        screen_handler = logging.StreamHandler(stream=sys.stdout)
        screen_handler.setFormatter(formatter)
        logger = logging.getLogger(cls.__name__)
        logger.setLevel(logging.DEBUG)
        logger.addHandler(screen_handler)

        if log_file:
            file_handler = logging.FileHandler(realpath(log_file), mode="w")
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)

        return logger

    @classmethod
    def _valid_column_type(cls, column_type):
        return cls.COLUMN_PATTERN.match(column_type.strip())

    @classmethod
    def _column_type_length(cls, column_type):
        suffix = cls.COLUMN_LENGTH_PATTERN.search(column_type)
        if suffix:
            return suffix.group(0)
        return ""

    @classmethod  # noqa: ignore=C901
    def _translate_type_from_mysql_to_sqlite(
        cls, column_type  # pylint: disable=C0330
    ):  # pylint: disable=R0911,R0912
        """This could be optimized even further, however is seems adequate."""
        match = cls._valid_column_type(column_type)
        if not match:
            raise ValueError("Invalid column_type!")

        data_type = match.group(0).upper()
        if data_type == "TINYINT":
            return "TINYINT"
        if data_type == "SMALLINT":
            return "SMALLINT"
        if data_type == "MEDIUMINT":
            return "MEDIUMINT"
        if data_type in {"INT", "INTEGER"}:
            return "INTEGER"
        if data_type == "BIGINT":
            return "BIGINT"
        if data_type == "DOUBLE":
            return "DOUBLE"
        if data_type == "FLOAT":
            return "FLOAT"
        if data_type in {"DECIMAL", "YEAR", "TIME", "NUMERIC"}:
            return "NUMERIC"
        if data_type == "REAL":
            return "REAL"
        if data_type in {"DATETIME", "TIMESTAMP"}:
            return "DATETIME"
        if data_type == "DATE":
            return "DATE"
        if data_type in {
            "BIT",  # pylint: disable=C0330
            "BINARY",  # pylint: disable=C0330
            "BLOB",  # pylint: disable=C0330
            "LONGBLOB",  # pylint: disable=C0330
            "MEDIUMBLOB",  # pylint: disable=C0330
            "TINYBLOB",  # pylint: disable=C0330
            "VARBINARY",  # pylint: disable=C0330
        }:
            return "BLOB"
        if data_type == "BOOLEAN":
            return "BOOLEAN"
        if data_type == "CHAR":
            return "CHARACTER" + cls._column_type_length(column_type)
        if data_type == "NCHAR":
            return "NCHAR" + cls._column_type_length(column_type)
        if data_type == "NVARCHAR":
            return "NVARCHAR" + cls._column_type_length(column_type)
        if data_type == "VARCHAR":
            return "VARCHAR" + cls._column_type_length(column_type)
        return "TEXT"

    def _build_create_table_sql(self, table_name):
        sql = 'CREATE TABLE IF NOT EXISTS "{}" ('.format(table_name)
        primary = "PRIMARY KEY ("
        has_primary_key = False
        indices = ""

        self._mysql_cur_dict.execute("SHOW COLUMNS FROM `{}`".format(table_name))

        for row in self._mysql_cur_dict.fetchall():
            sql += ' "{name}" {type} {notnull}, '.format(
                name=row["Field"],
                type=self._translate_type_from_mysql_to_sqlite(row["Type"]),
                notnull="NULL" if row["Null"] == "YES" else "NOT NULL",
            )
            if row["Key"] in {"PRI", "UNI", "MUL"}:
                if row["Key"] == "PRI":
                    has_primary_key = True
                    primary += '"{name}", '.format(name=row["Field"])
                else:
                    indices += """ CREATE {unique} INDEX {table_name}_{column_slug_name}_IDX ON "{table_name}" ("{column_name}");""".format(  # noqa: ignore=E501  # pylint: disable=C0301
                        unique="UNIQUE" if row["Key"] == "UNI" else "",
                        table_name=table_name,
                        column_slug_name=slugify(row["Field"], separator="_"),
                        column_name=row["Field"],
                    )
        if has_primary_key:
            sql += primary.rstrip(", ")
            sql += ")"
        sql = sql.rstrip(", ")
        sql += ");"
        sql += indices
        return " ".join(sql.split())

    def _create_table(self, table_name, attempting_reconnect=False):
        try:
            if attempting_reconnect:
                self._mysql.reconnect()
            self._sqlite_cur.executescript(self._build_create_table_sql(table_name))
            self._sqlite.commit()
        except mysql.connector.Error as err:
            if err.errno == errorcode.CR_SERVER_LOST:
                if not attempting_reconnect:
                    self._logger.warning(
                        "Connection to MySQL server lost." "\nAttempting to reconnect."
                    )
                    self._create_table(table_name, True)
                else:
                    self._logger.warning(
                        "Connection to MySQL server lost."
                        "\nReconnection attempt aborted."
                    )
                    raise
            self._logger.error(
                "_create_table failed creating table %s: %s", table_name, err
            )
            raise
        except sqlite3.Error as err:
            self._logger.error(
                "_create_table failed creating table %s: %s", table_name, err
            )
            raise

    def _transfer_table_data(  # pylint: disable=C0330
        self, table_name, sql, total_records=0, attempting_reconnect=False
    ):
        if attempting_reconnect:
            self._mysql.reconnect()
        try:
            if self._chunk_size is not None and self._chunk_size > 0:
                for chunk in trange(
                    self._current_chunk_number,  # pylint: disable=C0330
                    int(
                        ceil(total_records / self._chunk_size)
                    ),  # pylint: disable=C0330
                ):
                    self._current_chunk_number = chunk
                    self._sqlite_cur.executemany(
                        sql,
                        (
                            tuple(
                                col.decode() if col is not None else None for col in row
                            )
                            for row in self._mysql_cur.fetchmany(self._chunk_size)
                        ),
                    )
            else:
                self._sqlite_cur.executemany(
                    sql,
                    (
                        tuple(col.decode() if col is not None else None for col in row)
                        for row in self._mysql_cur.fetchall()
                    ),
                )
            self._sqlite.commit()
        except mysql.connector.Error as err:
            if err.errno == errorcode.CR_SERVER_LOST:
                if not attempting_reconnect:
                    self._logger.warning(
                        "Connection to MySQL server lost.\nAttempting to reconnect."
                    )
                    self._transfer_table_data(
                        table_name=table_name,
                        sql=sql,
                        total_records=total_records,
                        attempting_reconnect=True,
                    )
                else:
                    self._logger.warning(
                        "Connection to MySQL server lost.\nReconnection attempt aborted."  # noqa: ignore=E501  # pylint: disable=C0301
                    )
                    raise
            self._logger.error(
                "transfer failed inserting data into table %s: %s", table_name, err
            )
            raise
        except sqlite3.Error as err:
            self._logger.error(
                "transfer failed inserting data into table %s: %s", table_name, err
            )
            raise

    def transfer(self):
        """The primary and only method with which we transfer all the data."""
        self._mysql_cur.execute("SHOW TABLES")

        for row in self._mysql_cur.fetchall():
            # reset the chunk
            self._current_chunk_number = 0

            # create the table
            table_name = row[0].decode()
            self._create_table(table_name)

            # get the size of the data
            self._mysql_cur_dict.execute(
                "SELECT COUNT(*) AS `total_records` FROM `{}`".format(table_name)
            )
            total_records = int(self._mysql_cur_dict.fetchone()["total_records"])

            # only continue if there is anything to transfer
            if total_records > 0:
                # populate it
                self._logger.info("Transferring table %s", table_name)
                self._mysql_cur.execute("SELECT * FROM `{}`".format(table_name))
                columns = [column[0] for column in self._mysql_cur.description]
                # build the SQL string
                sql = 'INSERT OR IGNORE INTO "{table}" ({fields}) VALUES ({placeholders})'.format(  # noqa: ignore=E501  # pylint: disable=C0301
                    table=table_name,
                    fields=('"{}", ' * len(columns)).rstrip(" ,").format(*columns),
                    placeholders=("?, " * len(columns)).rstrip(" ,"),
                )
                self._transfer_table_data(
                    table_name=table_name, sql=sql, total_records=total_records
                )

        if self._vacuum:
            self._logger.info(
                "Vacuuming created SQLite database file.\nThis might take a while."
            )
            self._sqlite_cur.execute("VACUUM")

        self._logger.info("Done!")