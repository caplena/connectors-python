#
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one
# or more contributor license agreements. Licensed under the Elastic License 2.0;
# you may not use this file except in compliance with the Elastic License 2.0.
#
"""Tests the Network Drive source class methods.
"""
import asyncio
import datetime
from io import BytesIO
from unittest import mock
from unittest.mock import ANY

import pytest
import smbclient
from smbprotocol.exceptions import LogonFailure, SMBOSError

from connectors.filtering.validation import SyncRuleValidationResult
from connectors.protocol import Filter
from connectors.sources.network_drive import (
    NASDataSource,
    NetworkDriveAdvancedRulesValidator,
)
from tests.commons import AsyncIterator
from tests.sources.support import create_source

READ_COUNT = 0
MAX_CHUNK_SIZE = 65536
ADVANCED_SNIPPET = "advanced_snippet"


def mock_file(name):
    """Generates the smbprotocol object for a file

    Args:
        name (str): The name of the mocked file
    """
    mock_response = mock.Mock()
    mock_response.name = name
    mock_response.path = f"\\1.2.3.4/dummy_path/{name}"
    mock_stats = {}
    mock_stats["file_id"] = mock.Mock()
    mock_stats["file_id"].get_value.return_value = "1"
    mock_stats["allocation_size"] = mock.Mock()
    mock_stats["allocation_size"].get_value.return_value = "30"
    mock_stats["creation_time"] = mock.Mock()
    mock_stats["creation_time"].get_value.return_value = datetime.datetime(
        2022, 1, 11, 12, 12, 30
    )
    mock_stats["change_time"] = mock.Mock()
    mock_stats["change_time"].get_value.return_value = datetime.datetime(
        2022, 4, 21, 12, 12, 30
    )

    mock_response._dir_info.fields = mock_stats
    mock_response.is_dir.return_value = False

    return mock_response


def mock_folder(name):
    """Generates the smbprotocol object for a folder

    Args:
        name (str): The name of the mocked folder
    """
    mock_response = mock.Mock()
    mock_response.name = name
    mock_response.path = f"\\1.2.3.4/dummy_path/{name}"
    mock_response.is_dir.return_value = True
    mock_stats = {}
    mock_stats["file_id"] = mock.Mock()
    mock_stats["file_id"].get_value.return_value = "122"
    mock_stats["allocation_size"] = mock.Mock()
    mock_stats["allocation_size"].get_value.return_value = "200"
    mock_stats["creation_time"] = mock.Mock()
    mock_stats["creation_time"].get_value.return_value = datetime.datetime(
        2022, 2, 11, 12, 12, 30
    )
    mock_stats["change_time"] = mock.Mock()
    mock_stats["change_time"].get_value.return_value = datetime.datetime(
        2022, 5, 21, 12, 12, 30
    )
    mock_response._dir_info.fields = mock_stats
    return mock_response


def side_effect_function(MAX_CHUNK_SIZE):
    """Dynamically changing return values during reading a file in chunks
    Args:
        MAX_CHUNK_SIZE: Maximum bytes allowed to be read at a given time
    """
    global READ_COUNT
    if READ_COUNT:
        return None
    READ_COUNT += 1
    return b"Mock...."


@pytest.mark.asyncio
async def test_ping_for_successful_connection():
    """Tests the ping functionality for ensuring connection to the Network Drive."""
    # Setup
    expected_response = True
    response = asyncio.Future()
    response.set_result(expected_response)

    # Execute
    with mock.patch.object(smbclient, "register_session", return_value=response):
        async with create_source(NASDataSource) as source:
            await source.ping()


@pytest.mark.asyncio
@mock.patch("smbclient.register_session")
async def test_ping_for_failed_connection(session_mock):
    """Tests the ping functionality when connection can not be established to Network Drive.

    Args:
        session_mock (patch): The patch of register_session method
    """
    # Setup
    response = asyncio.Future()
    response.set_result(None)
    session_mock.side_effect = ValueError
    async with create_source(NASDataSource) as source:
        # Execute
        with pytest.raises(Exception):
            await source.ping()


@pytest.mark.asyncio
@mock.patch("smbclient.register_session")
async def test_create_connection_with_invalid_credentials(session_mock):
    """Tests the create_connection fails with invalid credentials

    Args:
        session_mock (patch): The patch of register_session method
    """
    # Setup
    async with create_source(NASDataSource) as source:
        session_mock.side_effect = LogonFailure

        # Execute
        with pytest.raises(LogonFailure):
            source.create_connection()


@mock.patch("smbclient.scandir")
@pytest.mark.asyncio
async def test_get_files_with_invalid_path(dir_mock):
    """Tests the scandir method of smbclient throws error on invalid path

    Args:
        dir_mock (patch): The patch of scandir method
    """
    # Setup
    async with create_source(NASDataSource) as source:
        path = "unknown_path"
        dir_mock.side_effect = SMBOSError(ntstatus=3221225487, filename="unknown_path")

        # Execute
        async for file in source.get_files(path=path):
            assert file == []


@pytest.mark.asyncio
@mock.patch("smbclient.scandir")
async def test_get_files(dir_mock):
    """Tests the get_files method for network drive

    Args:
        dir_mock (patch): The patch of scandir method
    """
    # Setup
    async with create_source(NASDataSource) as source:
        path = "\\1.2.3.4/dummy_path"
        dir_mock.return_value = [mock_file(name="a1.md"), mock_folder(name="A")]
        expected_output = [
            {
                "_id": "1",
                "_timestamp": "2022-04-21T12:12:30",
                "path": "\\1.2.3.4/dummy_path/a1.md",
                "title": "a1.md",
                "created_at": "2022-01-11T12:12:30",
                "size": "30",
                "type": "file",
            },
            {
                "_id": "122",
                "_timestamp": "2022-05-21T12:12:30",
                "path": "\\1.2.3.4/dummy_path/A",
                "title": "A",
                "created_at": "2022-02-11T12:12:30",
                "size": "200",
                "type": "folder",
            },
        ]

        # Execute
        response = []
        async for file in source.get_files(path=path):
            response.append(file)

        # Assert
        assert response == expected_output


@pytest.mark.asyncio
@mock.patch("smbclient.open_file")
async def test_fetch_file_when_file_is_inaccessible(file_mock):
    """Tests the open_file method of smbclient throws error when file cannot be accessed

    Args:
        file_mock (patch): The patch of open_file method
    """
    # Setup
    async with create_source(NASDataSource) as source:
        path = "\\1.2.3.4/Users/file1.txt"
        file_mock.side_effect = SMBOSError(ntstatus=0xC0000043, filename="file1.txt")

        # Execute
        response = source.fetch_file_content(path=path)

        # Assert
        assert response is None


@pytest.mark.asyncio
@mock.patch("smbclient.open_file")
async def test_get_content(file_mock):
    """Test get_content method of Network Drive

    Args:
        file_mock (patch): The patch of open_file method
    """
    # Setup
    async with create_source(NASDataSource) as source:
        file_mock.return_value.__enter__.return_value.read.return_value = bytes(
            "Mock....", "utf-8"
        )

        mock_response = {
            "id": "1",
            "_timestamp": "2022-04-21T12:12:30",
            "title": "file1.txt",
            "path": "\\1.2.3.4/Users/folder1/file1.txt",
            "size": "50",
        }

        mocked_content_response = BytesIO(b"Mock....")

        expected_output = {
            "_id": "1",
            "_timestamp": "2022-04-21T12:12:30",
            "_attachment": "TW9jay4uLi4=",
        }

        # Execute
        source.fetch_file_content = mock.MagicMock(return_value=mocked_content_response)
        actual_response = await source.get_content(mock_response, doit=True)

        # Assert
        assert actual_response == expected_output


@pytest.mark.asyncio
@mock.patch("smbclient.open_file")
async def test_get_content_with_upper_extension(file_mock):
    """Test get_content method of Network Drive

    Args:
        file_mock (patch): The patch of open_file method
    """
    # Setup
    async with create_source(NASDataSource) as source:
        file_mock.return_value.__enter__.return_value.read.return_value = bytes(
            "Mock....", "utf-8"
        )

        mock_response = {
            "id": "1",
            "_timestamp": "2022-04-21T12:12:30",
            "title": "file1.TXT",
            "path": "\\1.2.3.4/Users/folder1/file1.txt",
            "size": "50",
        }

        mocked_content_response = BytesIO(b"Mock....")

        expected_output = {
            "_id": "1",
            "_timestamp": "2022-04-21T12:12:30",
            "_attachment": "TW9jay4uLi4=",
        }

        # Execute
        source.fetch_file_content = mock.MagicMock(return_value=mocked_content_response)
        actual_response = await source.get_content(mock_response, doit=True)

        # Assert
        assert actual_response == expected_output


@pytest.mark.asyncio
async def test_get_content_when_doit_false():
    """Test get_content method when doit is false."""
    # Setup
    async with create_source(NASDataSource) as source:
        mock_response = {
            "id": "1",
            "_timestamp": "2022-04-21T12:12:30",
            "title": "file1.txt",
        }

        # Execute
        actual_response = await source.get_content(mock_response)

        # Assert
        assert actual_response is None


@pytest.mark.asyncio
async def test_get_content_when_file_size_is_large():
    """Test the module responsible for fetching the content of the file if it is not extractable"""
    # Setup
    async with create_source(NASDataSource) as source:
        mock_response = {
            "id": "1",
            "_timestamp": "2022-04-21T12:12:30",
            "title": "file1.txt",
            "size": "20000000000",
        }

        # Execute
        actual_response = await source.get_content(mock_response, doit=True)

        # Assert
        assert actual_response is None


@pytest.mark.asyncio
async def test_get_content_when_file_type_not_supported():
    """Test get_content method when the file content type is not supported"""
    # Setup
    async with create_source(NASDataSource) as source:
        mock_response = {
            "id": "1",
            "_timestamp": "2022-04-21T12:12:30",
            "title": "file2.dmg",
        }

        # Execute
        actual_response = await source.get_content(mock_response, doit=True)

        # Assert
        assert actual_response is None


@pytest.mark.asyncio
@mock.patch.object(NASDataSource, "get_files", return_value=mock.MagicMock())
@mock.patch("smbclient.walk")
async def test_get_doc(mock_get_files, mock_walk):
    """Test get_doc method of NASDataSource Class

    Args:
        mock_get_files (coroutine): The patch of get_files coroutine
        mock_walk (patch): The patch of walk method of smbclient
    """
    # Setup
    async with create_source(NASDataSource) as source:
        # Execute
        async for _, _ in source.get_docs():
            # Assert
            mock_get_files.assert_awaited()


@pytest.mark.asyncio
@mock.patch("smbclient.open_file")
async def test_fetch_file_when_file_is_accessible(file_mock):
    """Tests the open_file method of smbclient when file can be accessed

    Args:
        file_mock (patch): The patch of open_file method
    """
    # Setup
    async with create_source(NASDataSource) as source:
        path = "\\1.2.3.4/Users/file1.txt"

        file_mock.return_value.__enter__.return_value.read = mock.MagicMock(
            side_effect=side_effect_function
        )

        # Execute
        response = source.fetch_file_content(path=path)

        # Assert
        assert response.read() == b"Mock...."


@pytest.mark.asyncio
async def test_close_without_session():
    async with create_source(NASDataSource) as source:
        await source.close()

    assert source.session is None


@pytest.mark.parametrize(
    "advanced_rules, expected_validation_result",
    [
        (
            # valid: empty array should be valid
            [],
            SyncRuleValidationResult.valid_result(
                SyncRuleValidationResult.ADVANCED_RULES
            ),
        ),
        (
            # valid: empty object should also be valid -> default value in Kibana
            {},
            SyncRuleValidationResult.valid_result(
                SyncRuleValidationResult.ADVANCED_RULES
            ),
        ),
        (
            # valid: one custom pattern
            [{"pattern": "a/b"}],
            SyncRuleValidationResult.valid_result(
                SyncRuleValidationResult.ADVANCED_RULES
            ),
        ),
        (
            # valid: two custom patterns
            [{"pattern": "a/b/*"}, {"pattern": "a/*"}],
            SyncRuleValidationResult.valid_result(
                SyncRuleValidationResult.ADVANCED_RULES
            ),
        ),
        (
            # invalid: pattern empty
            [{"pattern": "a/**/c"}, {"pattern": ""}],
            SyncRuleValidationResult(
                SyncRuleValidationResult.ADVANCED_RULES,
                is_valid=False,
                validation_message=ANY,
            ),
        ),
        (
            # invalid: unallowed key
            [{"pattern": "a/b/*"}, {"queries": "a/d"}],
            SyncRuleValidationResult(
                SyncRuleValidationResult.ADVANCED_RULES,
                is_valid=False,
                validation_message=ANY,
            ),
        ),
        (
            # invalid: list of strings -> wrong type
            {"pattern": ["a/b/*"]},
            SyncRuleValidationResult(
                SyncRuleValidationResult.ADVANCED_RULES,
                is_valid=False,
                validation_message=ANY,
            ),
        ),
        (
            # invalid: array of arrays -> wrong type
            {"path": ["a/b/c", ""]},
            SyncRuleValidationResult(
                SyncRuleValidationResult.ADVANCED_RULES,
                is_valid=False,
                validation_message=ANY,
            ),
        ),
    ],
)
@pytest.mark.asyncio
async def test_advanced_rules_validation(advanced_rules, expected_validation_result):
    mock_data = [
        ("\\1.2.3.4/a", ["d.txt"], ["b"]),
        ("\\1.2.3.4/a/b", ["c.txt"], ["e"]),
        ("\\1.2.3.4/a/b/e", [], []),
    ]
    async with create_source(NASDataSource) as source:
        with mock.patch.object(smbclient, "register_session"):
            with mock.patch.object(
                smbclient, "walk", side_effect=[iter(mock_data), iter(mock_data)]
            ):
                validation_result = await NetworkDriveAdvancedRulesValidator(
                    source
                ).validate(advanced_rules)

                assert validation_result == expected_validation_result


@pytest.mark.parametrize(
    "filtering",
    [
        Filter(
            {
                ADVANCED_SNIPPET: {
                    "value": [
                        {"pattern": "training/python/async"},
                        {"pattern": "training/**/examples"},
                    ]
                }
            }
        ),
    ],
)
@pytest.mark.asyncio
async def test_get_docs_with_advanced_rules(filtering):
    async with create_source(NASDataSource) as source:
        response_list = []
        mock_data = [
            ("\\1.2.3.4/training", ["d.txt", "a.txt"], ["java", "python"]),
            ("\\1.2.3.4/training/python", ["c.txt"], ["basics", "async"]),
            ("\\1.2.3.4/training/python/async", ["coroutines.py"], []),
            ("\\1.2.3.4/training/python/basics", [], ["examples"]),
            ("\\1.2.3.4/training/python/basics/examples", ["lecture.py"], []),
            (
                "\\1.2.3.4/training/java",
                ["hello.java", "inheritance.java", "collections.java"],
                [],
            ),
        ]
        with mock.patch.object(
            smbclient, "walk", side_effect=[iter(mock_data), iter(mock_data)]
        ):
            with mock.patch.object(
                NASDataSource,
                "get_files",
                side_effect=[
                    AsyncIterator(
                        [
                            {
                                "path": "\\1.2.3.4/training/python/basics/examples/lecture.py",
                                "size": "2700",
                                "_id": "1233",
                                "created_at": "1111-11-11T11:11:11",
                                "type": "file",
                                "title": "lecture.py",
                                "_timestamp": "1212-12-12T12:12:12",
                            },
                        ]
                    ),
                    AsyncIterator(
                        [
                            {
                                "path": "\\1.2.3.4/training/python/async/coroutines.py",
                                "size": "30000",
                                "_id": "987",
                                "created_at": "1111-11-11T11:11:11",
                                "type": "file",
                                "title": "coroutines.py",
                                "_timestamp": "1212-12-12T12:12:12",
                            },
                        ]
                    ),
                ],
            ):
                async for response in source.get_docs(filtering):
                    response_list.append(response[0])
        assert [
            {
                "path": "\\1.2.3.4/training/python/basics/examples/lecture.py",
                "size": "2700",
                "_id": "1233",
                "created_at": "1111-11-11T11:11:11",
                "type": "file",
                "title": "lecture.py",
                "_timestamp": "1212-12-12T12:12:12",
            },
            {
                "path": "\\1.2.3.4/training/python/async/coroutines.py",
                "size": "30000",
                "_id": "987",
                "created_at": "1111-11-11T11:11:11",
                "type": "file",
                "title": "coroutines.py",
                "_timestamp": "1212-12-12T12:12:12",
            },
        ] == response_list
