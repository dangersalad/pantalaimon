# Copyright 2019 The Matrix.org Foundation CIC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import os
from collections import defaultdict
from typing import List, Optional, Tuple

import attr
from nio.crypto import TrustState
from nio.store import (
    Accounts,
    DeviceKeys,
    DeviceTrustState,
    use_database,
    use_database_atomic,
)
from peewee import SQL, DoesNotExist, ForeignKeyField, Model, SqliteDatabase, TextField


@attr.s
class FetchTask:
    room_id = attr.ib(type=str)
    token = attr.ib(type=str)


class DictField(TextField):
    def python_value(self, value):  # pragma: no cover
        return json.loads(value)

    def db_value(self, value):  # pragma: no cover
        return json.dumps(value)


class AccessTokens(Model):
    token = TextField()
    account = ForeignKeyField(
        model=Accounts, primary_key=True, backref="access_token", on_delete="CASCADE"
    )


class Servers(Model):
    name = TextField()

    class Meta:
        constraints = [SQL("UNIQUE(name)")]


class ServerUsers(Model):
    user_id = TextField()
    server = ForeignKeyField(
        model=Servers, column_name="server_id", backref="users", on_delete="CASCADE"
    )

    class Meta:
        constraints = [SQL("UNIQUE(user_id,server_id)")]


class PanSyncTokens(Model):
    token = TextField()
    user = ForeignKeyField(model=ServerUsers, column_name="user_id")

    class Meta:
        constraints = [SQL("UNIQUE(user_id)")]


class PanFetcherTasks(Model):
    user = ForeignKeyField(
        model=ServerUsers, column_name="user_id", backref="fetcher_tasks"
    )
    room_id = TextField()
    token = TextField()

    class Meta:
        constraints = [SQL("UNIQUE(user_id, room_id, token)")]


@attr.s
class ClientInfo:
    user_id = attr.ib(type=str)
    access_token = attr.ib(type=str)


@attr.s
class PanStore:
    store_path = attr.ib(type=str)
    database_name = attr.ib(type=str, default="pan.db")
    database = attr.ib(type=SqliteDatabase, init=False)
    database_path = attr.ib(type=str, init=False)
    models = [
        Accounts,
        AccessTokens,
        Servers,
        ServerUsers,
        DeviceKeys,
        DeviceTrustState,
        PanSyncTokens,
        PanFetcherTasks,
    ]

    def __attrs_post_init__(self):
        self.database_path = os.path.join(
            os.path.abspath(self.store_path), self.database_name
        )

        self.database = self._create_database()
        self.database.connect()

        with self.database.bind_ctx(self.models):
            self.database.create_tables(self.models)

    def _create_database(self):
        return SqliteDatabase(
            self.database_path, pragmas={"foreign_keys": 1, "secure_delete": 1}
        )

    @use_database
    def _get_account(self, user_id, device_id):
        try:
            return Accounts.get(
                Accounts.user_id == user_id, Accounts.device_id == device_id
            )
        except DoesNotExist:
            return None

    @use_database_atomic
    def replace_fetcher_task(self, server, pan_user, old_task, new_task):
        server = Servers.get(name=server)
        user = ServerUsers.get(server=server, user_id=pan_user)

        PanFetcherTasks.delete().where(
            PanFetcherTasks.user == user,
            PanFetcherTasks.room_id == old_task.room_id,
            PanFetcherTasks.token == old_task.token,
        ).execute()

        PanFetcherTasks.replace(
            user=user, room_id=new_task.room_id, token=new_task.token
        ).execute()

    @use_database
    def save_fetcher_task(self, server, pan_user, task):
        server = Servers.get(name=server)
        user = ServerUsers.get(server=server, user_id=pan_user)

        PanFetcherTasks.replace(
            user=user, room_id=task.room_id, token=task.token
        ).execute()

    def load_fetcher_tasks(self, server, pan_user):
        server = Servers.get(name=server)
        user = ServerUsers.get(server=server, user_id=pan_user)

        tasks = []

        for t in user.fetcher_tasks:
            tasks.append(FetchTask(t.room_id, t.token))

        return tasks

    def delete_fetcher_task(self, server, pan_user, task):
        server = Servers.get(name=server)
        user = ServerUsers.get(server=server, user_id=pan_user)

        PanFetcherTasks.delete().where(
            PanFetcherTasks.user == user,
            PanFetcherTasks.room_id == task.room_id,
            PanFetcherTasks.token == task.token,
        ).execute()

    @use_database
    def save_token(self, server, pan_user, token):
        # type: (str, str, str) -> None
        """Save a sync token for a pan user."""
        server = Servers.get(name=server)
        user = ServerUsers.get(server=server, user_id=pan_user)

        PanSyncTokens.replace(user=user, token=token).execute()

    @use_database
    def load_token(self, server, pan_user):
        # type: (str, str) -> Optional[str]
        """Load a sync token for a pan user.

        Returns the sync token if one is found.
        """
        server = Servers.get(name=server)
        user = ServerUsers.get(server=server, user_id=pan_user)

        token = PanSyncTokens.get_or_none(user=user)

        if token:
            return token.token

        return None

    @use_database
    def save_server_user(self, server_name, user_id):
        # type: (str, str) -> None
        server, _ = Servers.get_or_create(name=server_name)

        ServerUsers.insert(
            user_id=user_id, server=server
        ).on_conflict_ignore().execute()

    @use_database
    def load_all_users(self):
        users = []

        query = Accounts.select(Accounts.user_id, Accounts.device_id)

        for account in query:
            users.append((account.user_id, account.device_id))

        return users

    @use_database
    def load_users(self, server_name):
        # type: (str) -> List[Tuple[str, str]]
        users = []

        server = Servers.get_or_none(Servers.name == server_name)

        if not server:
            return []

        server_users = []

        for u in server.users:
            server_users.append(u.user_id)

        query = Accounts.select(Accounts.user_id, Accounts.device_id).where(
            Accounts.user_id.in_(server_users)
        )

        for account in query:
            users.append((account.user_id, account.device_id))

        return users

    @use_database
    def save_access_token(self, user_id, device_id, access_token):
        account = self._get_account(user_id, device_id)
        assert account

        AccessTokens.replace(account=account, token=access_token).execute()

    @use_database
    def load_access_token(self, user_id, device_id):
        # type: (str, str) -> Optional[str]
        account = self._get_account(user_id, device_id)

        if not account:
            return None

        try:
            return account.access_token[0].token
        except IndexError:
            return None

    @use_database
    def load_all_devices(self):
        # type (str, str) -> Dict[str, Dict[str, DeviceStore]]
        store = dict()

        query = Accounts.select()

        for account in query:
            device_store = defaultdict(dict)

            for d in account.device_keys:

                if d.deleted:
                    continue

                try:
                    trust_state = d.trust_state[0].state
                except IndexError:
                    trust_state = TrustState.unset

                keys = {k.key_type: k.key for k in d.keys}

                device_store[d.user_id][d.device_id] = {
                    "user_id": d.user_id,
                    "device_id": d.device_id,
                    "ed25519": keys["ed25519"],
                    "curve25519": keys["curve25519"],
                    "trust_state": trust_state.name,
                    "device_display_name": d.display_name,
                }

            store[account.user_id] = device_store

        return store
