"""
test_cleaner.py
===============
"""
from datetime import datetime, timedelta
from datetime import time
import shutil

from freezegun import freeze_time
import cleaner as c
from pyfakefs.fake_filesystem import FakeFilesystem
from pathlib import Path
from testing_common import *
import cleaner

# helper func
def _to_time(time) -> time:
    return datetime.strptime(time, "%H:%M").time()


def test_cleaner_no_syntax_errors():
    """Checks if cleaner.py can be started."""
    assert c


def test_is_not_offpeak():
    is_not = c._is_offpeak("22:00", "06:00", _to_time("21:00"))
    assert not is_not


def test_is_offpeak():
    is_ = c._is_offpeak("22:00", "06:00", _to_time("23:00"))
    assert is_


def test_other_input_offpeak():
    is_ = c._is_offpeak("22:00", "6:00", _to_time("5:00"))
    assert is_


def test_wrong_start_input_offpeak():
    is_ = c._is_offpeak("asdf", "6:00", _to_time("5:00"))
    assert is_


def test_wrong_end_input_offpeak():
    is_ = c._is_offpeak("22:00", "asdf", _to_time("5:00"))
    assert is_


def test_cleaning_simple(fs: FakeFilesystem, mercure_config, mocked):
    """ """
    config = mercure_config({"retention": timedelta(hours=24 * 3).total_seconds()})
    fs.set_disk_usage(10_000)

    success_folder = Path(config.success_folder)

    with freeze_time("2020-01-01 00:00:00") as frozen_time:  # Pretend it's January 1st
        (success_folder / "series_1").mkdir()
        frozen_time.tick(delta=timedelta(hours=24))  # Advance time by a day
        (success_folder / "series_2").mkdir()
        frozen_time.tick(delta=timedelta(hours=24))
        (success_folder / "series_3").mkdir()
        frozen_time.tick(delta=timedelta(hours=24))
        (success_folder / "series_4").mkdir()
        frozen_time.tick(delta=timedelta(hours=24))
        assert len(list(success_folder.glob("*"))) == 4
        cleaner.clean({})
        assert len(list(success_folder.glob("*"))) == 3
        frozen_time.tick(delta=timedelta(hours=24))
        cleaner.clean({})
        assert len(list(success_folder.glob("*"))) == 2
        frozen_time.tick(delta=timedelta(hours=24))
        cleaner.clean({})
        assert len(list(success_folder.glob("*"))) == 1
