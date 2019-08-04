import json
import logging
import os
import subprocess
import sys
import webbrowser
from typing import Dict, List, Optional, Tuple, TypeVar, Union
from urllib import parse

from galaxy.api.consts import Platform
from galaxy.api.errors import InvalidCredentials
from galaxy.api.plugin import create_and_run_plugin, Plugin
from galaxy.api.types import Authentication, Game, LicenseInfo, LicenseType, NextStep

from twitch_db_client import db_select, get_cookie


def is_windows() -> bool:
    return sys.platform == "win32"


if is_windows():
    import winreg

T = TypeVar("T")


def os_specific(unknown, win: Optional[T] = None, mac: Optional[T] = None) -> Optional[T]:
    return {"win32": win, "darwin": mac}.get(sys.platform, unknown)


class TwitchPlugin(Plugin):

    @staticmethod
    def _read_manifest() -> str:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "manifest.json")) as manifest:
            return json.load(manifest)

    @staticmethod
    def _get_client_install_path() -> Optional[str]:
        if is_windows():
            _CLIENT_DISPLAY_NAME = "Twitch"
            try:
                for h_root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                    with winreg.OpenKey(h_root, r"Software\Microsoft\Windows\CurrentVersion\Uninstall") as h_apps:
                        for idx in range(winreg.QueryInfoKey(h_apps)[0]):
                            try:
                                with winreg.OpenKeyEx(h_apps, winreg.EnumKey(h_apps, idx)) as h_app_info:
                                    def get_value(key):
                                        return winreg.QueryValueEx(h_app_info, key)[0]

                                    if get_value("DisplayName") == _CLIENT_DISPLAY_NAME:
                                        installer_path = get_value("InstallLocation")
                                        if os.path.exists(str(installer_path)):
                                            return installer_path

                            except (WindowsError, KeyError, ValueError):
                                continue

            except (WindowsError, KeyError, ValueError):
                logging.exception("Failed to get client install location")
                return None
        else:
            return None

    @property
    def _twitch_exe_path(self) -> Optional[str]:
        if not self._client_install_path:
            return None

        if is_windows():
            return os.path.join(self._client_install_path, "Bin", "Twitch.exe")

        return None

    @property
    def _db_cookies_path(self) -> Optional[str]:
        if not self._client_install_path:
            return None

        return os.path.join(self._client_install_path, "Electron3", "Cookies")

    @property
    def _db_owned_games(self) -> str:
        return str(os_specific(
            win=os.path.join(os.path.expandvars("%APPDATA%"), "Twitch", "Games", "Sql", "GameProductInfo.sqlite")
            , unknown=""
        ))

    def _get_user_info(self) -> Dict[str, str]:
        user_info_cookie = get_cookie(self._db_cookies_path, "twilight-user.desklight")
        if not user_info_cookie:
            return {}

        user_info = json.loads(parse.unquote(user_info_cookie))
        if not user_info:
            return {}

        return user_info

    def __init__(self, reader, writer, token):
        self._manifest = self._read_manifest()
        self._client_install_path = None
        super().__init__(Platform(self._manifest["platform"]), self._manifest["version"], reader, writer, token)

    def handshake_complete(self) -> None:
        self._client_install_path = self._get_client_install_path()

    def tick(self) -> None:
        if not self._client_install_path or not os.path.exists(self._client_install_path):
            self._client_install_path = self._get_client_install_path()

    async def authenticate(self, stored_credentials: Optional[Dict] = None) -> Union[NextStep, Authentication]:
        if not self._twitch_exe_path or not os.path.exists(self._twitch_exe_path):
            webbrowser.open_new_tab("https://www.twitch.tv/downloads")
            raise InvalidCredentials

        def get_auth_info() -> Optional[Tuple[str, str]]:
            if not self._db_cookies_path or not os.path.exists(self._db_cookies_path):
                logging.warning("No cookies db")
                return None

            user_info = self._get_user_info()
            if not user_info:
                logging.warning("No user info")
                return None

            user_id = user_info.get("id")
            user_name = user_info.get("displayName")

            if not user_id or not user_name:
                logging.warning("No user id/name")
                return None

            return user_id, user_name

        auth_info = get_auth_info()
        if not auth_info:
            subprocess.Popen(
                [self._twitch_exe_path]
                , creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
                , cwd=self._client_install_path
            )
            raise InvalidCredentials

        return Authentication(user_id=auth_info[0], user_name=auth_info[1])

    async def get_owned_games(self) -> List[Game]:
        try:
            return [
                Game(
                    game_id=row["ProductIdStr"]
                    , game_title=row["ProductTitle"]
                    , dlcs=None
                    , license_info=LicenseInfo(LicenseType.SinglePurchase)
                )
                for row in db_select(
                    db_path=self._db_owned_games
                    , query="select ProductIdStr, ProductTitle from DbSet"
                )
            ]
        except Exception:
            logging.exception("Failed to get owned games")
            return []


def main():
    create_and_run_plugin(TwitchPlugin, sys.argv)


if __name__ == "__main__":
    main()