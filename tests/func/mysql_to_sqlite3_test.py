import logging
import re
from collections import namedtuple

import mysql.connector
import pytest
import six
from _mysql_connector import MySQLInterfaceError, MySQLError
from mysql.connector import errorcode, MySQLConnection
from sqlalchemy import MetaData, Table, select, create_engine, inspect

from src.mysql_to_sqlite3 import MySQLtoSQLite

if six.PY2:
    from ..sixeptions import *


@pytest.mark.usefixtures("mysql_instance")
class TestMySQLtoSQLite:
    @pytest.mark.init
    def test_missing_mysql_user_raises_exception(self):
        with pytest.raises(ValueError) as excinfo:
            MySQLtoSQLite()
        assert "Please provide a MySQL user" in str(excinfo.value)

    @pytest.mark.init
    def test_missing_mysql_database_raises_exception(self, faker):
        with pytest.raises(ValueError) as excinfo:
            MySQLtoSQLite(mysql_user=faker.first_name().lower())
        assert "Please provide a MySQL database" in str(excinfo.value)

    @pytest.mark.init
    def test_invalid_mysql_credentials_raises_access_denied_exception(
        self, sqlite_database, mysql_database, mysql_credentials, faker
    ):
        with pytest.raises(mysql.connector.Error) as excinfo:
            MySQLtoSQLite(
                sqlite_file=sqlite_database,
                mysql_user=faker.first_name().lower(),
                mysql_password=faker.password(length=16),
                mysql_database=mysql_credentials.database,
                mysql_host=mysql_credentials.host,
                mysql_port=mysql_credentials.port,
            )
        assert "Access denied for user" in str(excinfo.value)

    @pytest.mark.init
    def test_bad_mysql_connection(self, sqlite_database, mysql_credentials, mocker):
        FakeConnector = namedtuple("FakeConnector", ["is_connected"])
        mocker.patch.object(
            mysql.connector,
            "connect",
            return_value=FakeConnector(is_connected=lambda: False),
        )
        with pytest.raises((ConnectionError, IOError)) as excinfo:
            MySQLtoSQLite(
                sqlite_file=sqlite_database,
                mysql_user=mysql_credentials.user,
                mysql_password=mysql_credentials.password,
                mysql_host=mysql_credentials.host,
                mysql_port=mysql_credentials.port,
                mysql_database=mysql_credentials.database,
                chunk=1000,
            )
        assert "Unable to connect to MySQL" in str(excinfo.value)

    @pytest.mark.init
    @pytest.mark.parametrize(
        "exception",
        [
            pytest.param(
                mysql.connector.Error(
                    msg="Unknown database 'test_db'", errno=errorcode.ER_BAD_DB_ERROR
                ),
                id="mysql.connector.Error",
            ),
            pytest.param(
                MySQLInterfaceError("Unknown database 'test_db'"),
                id="MySQLInterfaceError",
            ),
            pytest.param(MySQLError("Unknown database 'test_db'"), id="MySQLError"),
            pytest.param(Exception("Unknown database 'test_db'"), id="Exception"),
        ],
    )
    def test_non_existing_mysql_database_raises_exception(
        self,
        sqlite_database,
        mysql_database,
        mysql_credentials,
        faker,
        mocker,
        caplog,
        exception,
    ):
        class FakeMySQLConnection(MySQLConnection):
            @property
            def database(self):
                return self._database

            @database.setter
            def database(self, value):
                self._database = value
                # raise a fake exception
                raise exception

            def is_connected(self):
                return True

            def cursor(
                self,
                buffered=None,
                raw=None,
                prepared=None,
                cursor_class=None,
                dictionary=None,
                named_tuple=None,
            ):
                return True

        caplog.set_level(logging.DEBUG)
        mocker.patch.object(mysql.connector, "connect", return_value=FakeMySQLConnection())
        with pytest.raises(
            (mysql.connector.Error, MySQLInterfaceError, MySQLError, Exception)
        ) as excinfo:
            MySQLtoSQLite(
                sqlite_file=sqlite_database,
                mysql_user=mysql_credentials.user,
                mysql_password=mysql_credentials.password,
                mysql_database=mysql_credentials.database,
                mysql_host=mysql_credentials.host,
                mysql_port=mysql_credentials.port,
            )
            assert any(
                "MySQL Database does not exist!" in message
                for message in caplog.messages
            )
        assert "Unknown database" in str(excinfo.value)

    @pytest.mark.init
    def test_log_to_file(
        self, sqlite_database, mysql_database, mysql_credentials, caplog, tmpdir, faker
    ):
        log_file = tmpdir.join("db.log")
        caplog.set_level(logging.DEBUG)
        with pytest.raises(mysql.connector.Error):
            MySQLtoSQLite(
                sqlite_file=sqlite_database,
                mysql_user=faker.first_name().lower(),
                mysql_password=faker.password(length=16),
                mysql_database=mysql_credentials.database,
                mysql_host=mysql_credentials.host,
                mysql_port=mysql_credentials.port,
                log_file=str(log_file),
            )
        assert any("Access denied for user" in message for message in caplog.messages)
        with log_file.open("r") as log_fh:
            log = log_fh.read()
            assert caplog.messages[0] in log
            assert (
                re.match(r"^\d{4,}-\d{2,}-\d{2,}\s+\d{2,}:\d{2,}:\d{2,}\s+\w+\s+", log)
                is not None
            )

    @pytest.mark.transfer
    @pytest.mark.parametrize(
        "chunk, vacuum, buffered",
        [
            # 000
            pytest.param(
                None, False, False, id="no chunk, no vacuum, no buffered cursor"
            ),
            # 111
            pytest.param(1000, True, True, id="chunk, vacuum, buffered cursor"),
            # 110
            pytest.param(1000, True, False, id="chunk, vacuum, no buffered cursor"),
            # 011
            pytest.param(None, True, True, id="no chunk, vacuum, buffered cursor"),
            # 010
            pytest.param(None, True, False, id="no chunk, vacuum, no buffered cursor"),
            # 100
            pytest.param(1000, False, False, id="chunk, no vacuum, no buffered cursor"),
            # 001
            pytest.param(None, False, True, id="no chunk, no vacuum, buffered cursor"),
            # 101
            pytest.param(1000, False, True, id="chunk, no vacuum, buffered cursor"),
        ],
    )
    def test_transfer_transfers_all_tables_from_mysql_to_sqlite(
        self,
        sqlite_database,
        mysql_database,
        mysql_credentials,
        helpers,
        capsys,
        caplog,
        chunk,
        vacuum,
        buffered,
    ):
        proc = MySQLtoSQLite(
            sqlite_file=sqlite_database,
            mysql_user=mysql_credentials.user,
            mysql_password=mysql_credentials.password,
            mysql_database=mysql_credentials.database,
            mysql_host=mysql_credentials.host,
            mysql_port=mysql_credentials.port,
            chunk=chunk,
            vacuum=vacuum,
            buffered=buffered,
        )
        caplog.set_level(logging.DEBUG)
        proc.transfer()
        assert all(
            message in [record.message for record in caplog.records]
            for message in {
                "Transferring table article_authors",
                "Transferring table article_images",
                "Transferring table article_tags",
                "Transferring table articles",
                "Transferring table authors",
                "Transferring table images",
                "Transferring table tags",
                "Done!",
            }
        )
        assert all(record.levelname == "INFO" for record in caplog.records)
        assert not any(record.levelname == "ERROR" for record in caplog.records)
        out, err = capsys.readouterr()
        assert "Done!" in out.splitlines()[-1]

        sqlite_engine = create_engine(
            "sqlite:///{database}".format(database=sqlite_database)
        )
        sqlite_cnx = sqlite_engine.connect()
        sqlite_inspect = inspect(sqlite_engine)
        sqlite_tables = sqlite_inspect.get_table_names()
        mysql_engine = create_engine(
            "mysql+mysqlconnector://{user}:{password}@{host}:{port}/{database}".format(
                user=mysql_credentials.user,
                password=mysql_credentials.password,
                host=mysql_credentials.host,
                port=mysql_credentials.port,
                database=mysql_credentials.database,
            )
        )
        mysql_cnx = mysql_engine.connect()
        mysql_inspect = inspect(mysql_engine)
        mysql_tables = mysql_inspect.get_table_names()

        """ Test if both databases have the same table names """
        assert sqlite_tables == mysql_tables

        """ Test if all the tables have the same column names """
        for table_name in sqlite_tables:
            assert [
                column["name"] for column in sqlite_inspect.get_columns(table_name)
            ] == [column["name"] for column in mysql_inspect.get_columns(table_name)]

        """ Check if all the data was transferred correctly """
        sqlite_results = []
        mysql_results = []

        meta = MetaData(bind=None)
        for table_name in sqlite_tables:
            sqlite_table = Table(
                table_name, meta, autoload=True, autoload_with=sqlite_engine
            )
            sqlite_stmt = select([sqlite_table])
            sqlite_result = sqlite_cnx.execute(sqlite_stmt).fetchall()
            sqlite_result.sort()
            sqlite_results.append(sqlite_result)

        for table_name in mysql_tables:
            mysql_table = Table(
                table_name, meta, autoload=True, autoload_with=mysql_engine
            )
            mysql_stmt = select([mysql_table])
            mysql_result = mysql_cnx.execute(mysql_stmt).fetchall()
            mysql_result.sort()
            mysql_results.append(mysql_result)

        assert sqlite_results == mysql_results