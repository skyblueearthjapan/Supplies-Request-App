import pytest
from pydantic import ValidationError

from app import safety
from app.config import PROD_SPREADSHEET_ID_FIXED
from app.safety import UnsafeWriteError

from conftest import make_settings


def test_valid_staging_settings_load():
    s = make_settings()
    assert s.is_staging
    assert s.bootstrap_admin_list == ["imaizumi@lineworks-local.info"]
    assert s.oauth_redirect_uri == "https://supplies.lineworks-local.net/auth/callback"


@pytest.mark.parametrize(
    "overrides",
    [
        {"app_env": "dev"},
        {"database_url": ""},
        {"google_oauth_client_id": ""},
        {"session_secret": "short"},
        {"fernet_key": "not-a-fernet-key"},
        {"bootstrap_admins": ""},
        {"mail_redirect_to": ""},
        {"staging_spreadsheet_id": ""},
        {"staging_pdf_folder_id": ""},
        {"prod_pdf_folder_id": ""},
        {"staging_spreadsheet_id": PROD_SPREADSHEET_ID_FIXED},
        {"staging_pdf_folder_id": "prod-folder-id"},
        {"prod_spreadsheet_id": "something-else"},
        {"public_origin": "http://supplies.lineworks-local.net"},
        {"delegate_email": "someone@gmail.com"},
    ],
)
def test_missing_or_dangerous_settings_fail_closed(overrides):
    with pytest.raises(ValidationError):
        make_settings(**overrides)


def test_production_does_not_need_staging_values():
    s = make_settings(app_env="production", staging_spreadsheet_id="", staging_pdf_folder_id="", mail_redirect_to="")
    assert not s.is_staging


def test_staging_blocks_prod_spreadsheet():
    s = make_settings()
    with pytest.raises(UnsafeWriteError):
        safety.assert_writable_spreadsheet(PROD_SPREADSHEET_ID_FIXED, s)
    with pytest.raises(UnsafeWriteError):
        safety.assert_writable_spreadsheet("", s)
    assert safety.assert_writable_spreadsheet("staging-sheet-id", s) == "staging-sheet-id"


def test_production_allows_prod_spreadsheet():
    s = make_settings(app_env="production")
    assert safety.assert_writable_spreadsheet(PROD_SPREADSHEET_ID_FIXED, s) == PROD_SPREADSHEET_ID_FIXED
    assert safety.export_spreadsheet_id(s) == PROD_SPREADSHEET_ID_FIXED


def test_staging_export_goes_to_copy():
    assert safety.export_spreadsheet_id(make_settings()) == "staging-sheet-id"


def test_staging_blocks_prod_folder_and_its_subfolders():
    s = make_settings()
    with pytest.raises(UnsafeWriteError):
        safety.assert_writable_drive_folder("prod-folder-id", settings=s)
    with pytest.raises(UnsafeWriteError):
        safety.assert_writable_drive_folder("date-sub", ancestor_ids=["prod-folder-id"], settings=s)
    # ステージングのフォルダーの外（祖先が分からないもの）にも書かない
    with pytest.raises(UnsafeWriteError):
        safety.assert_writable_drive_folder("unknown-folder", settings=s)
    assert safety.assert_writable_drive_folder("date-sub", ancestor_ids=["staging-folder-id"], settings=s) == "date-sub"
    assert safety.assert_writable_drive_folder("staging-folder-id", settings=s) == "staging-folder-id"


def test_pdf_root_ignores_migrated_setting_in_staging():
    s = make_settings()
    assert safety.pdf_root_folder_id("prod-folder-id", s) == "staging-folder-id"
    p = make_settings(app_env="production")
    assert safety.pdf_root_folder_id("prod-folder-id", p) == "prod-folder-id"


def test_forced_mail_redirect():
    assert safety.forced_mail_redirect(make_settings()) == "imaizumi@lineworks-local.info"
    assert safety.forced_mail_redirect(make_settings(app_env="production")) == ""
