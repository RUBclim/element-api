import json
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated
from typing import Any
from typing import Literal
from typing import overload
from typing import TypeVar

import pandas as pd

from element.schemas import ApiReturn
from element.schemas import Device
from element.schemas import Folder
from element.schemas import Packet
from element.schemas import Reading


@dataclass
class _ValueRange:
    lo: float
    hi: float


T = TypeVar('T')


class ElementApi:
    def __init__(self, api_location: str, api_key: str) -> None:
        """
        Class to interact with the Elements API. The instance should, if
        possible, passed to functions so the internal cache can be utilized.

        :param api_location: The location where the Elements API is hosted
            including the version e.g. ``https://dew21.element-iot.com/api/v1``
        :param: api_key: The API key as provided to you
        """
        self.api_location = api_location.strip('/')
        self.api_key = api_key
        # the dict for caching looks like:
        # {'foldername': {'decentlab_id': 'address'}, ...}
        self._id_to_address_mapping: dict[str, dict[int, str]] = defaultdict(dict)  # noqa: E

    @property
    def _address_to_id_mapping(self) -> dict[str, dict[str, int]]:
        return {
            outer_k: {inner_v: inner_k for inner_k, inner_v in inner.items()}
            for outer_k, inner in self._id_to_address_mapping.items()
        }

    def decentlab_id_from_address(self, address: str, folder: str) -> int:
        """
        Get the decentlab id in the format of e.g. ``21680`` from the
        hexadecimal device address e.g. ``DEC0054B0``.

        :param address: the address of the device in a hexadecimal format as
            retrieved from the devices's mac-address e.g. ``DEC0054B0``
        """
        # try to get the mapping from the cached values of the instance
        # 1st check we have the folder:
        folder_mapping = self._address_to_id_mapping.get(folder)
        decentlab_id = folder_mapping.get(address) if folder_mapping else None
        # we don't know the id, try retrieving it from the API
        if decentlab_id is None:
            device = self.get_device(address)['body']
            decentlab_id = int(
                device['fields']['gerateinformation']['seriennummer'],
            )
            # we can also populate the cache this way
            self._id_to_address_mapping[folder][decentlab_id] = address

        return decentlab_id

    def address_from_decentlab_id(
            self,
            decentlab_id: int,
            folder: str,
    ) -> str:
        """
        Retrieve the address in the hexadecimal format e.g. ``DEC0054B0`` from
        the decenlab id e.g. ``21680``.

        The issue is, that the decentlab serial number/id is not part of the
        regular metadata in the IoT system. As far as we can see, you only get
        this when requesting actual data. Hence this may be really slow since
        in the worst case we have to go through all available stations.
        We try our best to cache this as part of the instance.

        :param decentlab_id: The decentlab serial nr/id in the format of e.g.
            ``21680``
        :param folder: The folder in the Elements IoT system to query for this
            this can be e.g. ``'stadt-dortmund-klimasensoren-inaktiv-sht35'``
        """
        # if we already have the mapping, simply return it without making any
        # requests to the API
        # we may not even have the folder cached
        folder_mapping = self._id_to_address_mapping.get(folder)
        if folder_mapping and folder_mapping.get(decentlab_id):
            return self._id_to_address_mapping[folder][decentlab_id]

        # first, get all available devices in the folder to potentially check
        # every single one of them manually.
        devices = self.get_devices(folder=folder)
        for i in devices['body']:
            curr_device_addr = i['name']
            # we can skip the ids we have already requested
            curr_folder = self._id_to_address_mapping.get(folder)
            if (
                    curr_folder and
                    curr_device_addr in self._id_to_address_mapping[folder].values()  # noqa: E501
            ):
                continue

            # now request some data from the device to get the device id
            resp = self.get_readings(
                device_name=curr_device_addr,
                limit=1,
                max_pages=1,
            )
            curr_decentlab_id = resp['body'][0]['data']['device_id']
            self._id_to_address_mapping[folder][curr_decentlab_id] = curr_device_addr  # noqa: E501
            if curr_decentlab_id == decentlab_id:
                return curr_device_addr
        else:
            raise ValueError(
                f'unable to find address for '
                f'station: {decentlab_id!r}',
            )

    def _make_req(
            self,
            route: str,
            params: dict[str, str | None | int] = {},
            max_pages: int | None = None,
    ) -> ApiReturn[T]:
        param_str = ''
        if params:
            param_str = f"&{'&'.join([f'{k}={v}' for k, v in params.items()])}"

        req = f'{self.api_location}/{route}?&auth={self.api_key}{param_str}'
        ret = urllib.request.urlopen(req, timeout=5)
        output_data: ApiReturn[T] = json.load(ret)
        # check if the request is paginated
        retrieve_after_id = output_data.get('retrieve_after_id')
        i = 1
        while retrieve_after_id:
            if max_pages and i >= max_pages:
                break
            req = f'{self.api_location}/{route}?&auth={self.api_key}{
                param_str
            }&retrieve_after={retrieve_after_id}'
            ret = urllib.request.urlopen(req, timeout=5)
            data = json.load(ret)
            retrieve_after_id = data.get('retrieve_after_id')
            if isinstance(output_data['body'], list):
                output_data['body'].extend(data['body'])
            else:
                raise TypeError(
                    'cannot handle pagination when `body` is not an array',
                )
            i += 1

        return output_data

    def get_folders(self) -> ApiReturn[list[Folder]]:
        """Get the folders from the API as the raw return values"""
        return self._make_req('tags')

    def get_folder_slugs(self) -> list[str]:
        """Get all available folder slugs. This can be:
        ``stadt-dortmund-klimasensoren-inaktiv-sht35``
        """
        ret = self.get_folders()
        return [i['slug'] for i in ret['body']]

    def get_devices(self, folder: str) -> ApiReturn[list[Device]]:
        """Get all available devices in the current ``folder``

        :param folder: The folder(-slug) to get the devices from
        """
        return self._make_req('/'.join(['tags', folder, 'devices']))

    def get_device_addresses(self, folder: str) -> list[str]:
        """Get the hexadecimal addresses e.g. ``DEC0054B0``from all available
        devices in the folder(-slug)

        :param folder: The folder(-slug) to get the devices from
        """
        devices = self.get_devices(folder=folder)
        return [d['name'] for d in devices['body']]

    def get_device(self, address: str) -> ApiReturn[Device]:
        """Get information for a single device via the hexadecimal address.

        :param address: the address of the device in a hexadecimal format as
            retrieved from the devices's mac-address e.g. ``DEC0054B0``, If
            only the ``decentlab_id`` is present, this may be retrieved using
            :method:`address_from_decentlab_id`.
        """
        return self._make_req('/'.join(['devices', address.lower()]))

    @overload
    def get_readings(
            self,
            device_name: str,
            *,
            sort: Literal['measured_at', 'inserted_at'] = 'measured_at',
            sort_direction: Literal['asc', 'desc'] = 'asc',
            start: datetime | None = None,
            end: datetime | None = None,
            limit: Annotated[int, _ValueRange(1, 100)] = 100,
            max_pages: int | None = None,
            as_dataframe: Literal[True],
    ) -> pd.DataFrame:
        ...

    @overload
    def get_readings(
            self,
            device_name: str,
            *,
            sort: Literal['measured_at', 'inserted_at'] = 'measured_at',
            sort_direction: Literal['asc', 'desc'] = 'asc',
            start: datetime | None = None,
            end: datetime | None = None,
            limit: Annotated[int, _ValueRange(1, 100)] = 100,
            max_pages: int | None = None,
            as_dataframe: Literal[False] = False,
    ) -> ApiReturn[list[Reading]]:
        ...

    def get_readings(
            self,
            device_name: str,
            *,
            sort: Literal['measured_at', 'inserted_at'] = 'measured_at',
            sort_direction: Literal['asc', 'desc'] = 'asc',
            start: datetime | None = None,
            end: datetime | None = None,
            limit: Annotated[int, _ValueRange(1, 100)] = 100,
            max_pages: int | None = None,
            as_dataframe: bool = False,
    ) -> ApiReturn[list[Reading]] | pd.DataFrame:
        """Get acutal readings from the API. This may be returned as the raw
        API-return-value or already converted to a dataframe.

        :param device_name: The name of the device as the hexadecimal address
            e.g. ``DEC0054B0``. If only the ``decentlab_id`` is present, this
            may be retrieved using :method:`address_from_decentlab_id`.
        :param sort: How the values should be sorted, currently this can only
            be ``measured_at`` or ``inserted_at``.
        :param sort_direction: The direction the sorting should be applied.
            Either ``asc`` for ascending or ``desc`` for descending.
        :param start: The datetime to start getting readings for. If ``None``,
            all available readings will be retrieved.
        :param end: The datetime to stop getting readings for. If ``None``,
            all available readings will be retrieved.
        :param limit: How many values to fetch per API request (must be between
            1 and 100).
        :param max_pages: After how many pages of pagination we stop, to avoid
            infinitely requesting data from the API.
        :param as_dataframe: Determines whether this function returns a pandas
            ``DataFrame`` or the raw API return (which is the default)
        """
        params: dict[str, Any] = {
            'sort': sort,
            'sort_direction': sort_direction,
            'limit': limit,
        }
        if start:
            params['after'] = start.isoformat()
        if end:
            params['before'] = end.isoformat()

        data: ApiReturn[list[Reading]] = self._make_req(
            '/'.join(['devices', 'by-name', device_name, 'readings']),
            params=params,
            max_pages=max_pages,
        )
        if as_dataframe:
            # we need to manually add the measured_at (datetime) columns
            df_data = [
                {'measured_at': i['measured_at'], **i['data']}
                for i in data['body']
            ]
            df = pd.DataFrame(df_data)
            if not df.empty:
                df['measured_at'] = pd.to_datetime(df['measured_at'])
                df = df.set_index('measured_at')
            else:
                print(f'no data for {device_name!r}')
            return df
        else:
            return data

    def get_packets_by_device(
            self,
            device_name: str,
            *,
            packet_type: Literal['up', 'down'] | None = None,
            start: datetime | None = None,
            end: datetime | None = None,
            limit: Annotated[int, _ValueRange(1, 100)] = 100,
            max_pages: int | None = None,
    ) -> ApiReturn[list[Packet]]:
        """Get the original packets from the API. This is returned as the raw
        API-return-value. The sorting is fixed to ``transceived_at``.

        :param device_name: The name of the device as the hexadecimal address
            e.g. ``DEC0054B0``. If only the ``decentlab_id`` is present, this
            may be retrieved using :method:`address_from_decentlab_id`.
        :param packet_type: Filter for packet_types (either ``up`` or ``down``)
            if ``None`` all package types are returned
        :param start: The datetime to start getting readings for. If ``None``,
            all available readings will be retrieved.
        :param end: The datetime to stop getting readings for. If ``None``,
            all available readings will be retrieved.
        :param limit: How many values to fetch per API request (must be between
            1 and 100).
        :param max_pages: After how many pages of pagination we stop, to avoid
            infinitely requesting data from the API.
        """
        params: dict[str, Any] = {'limit': limit}
        if packet_type:
            params['packet_type'] = packet_type
        if start:
            params['after'] = start.isoformat()
        if end:
            params['before'] = end.isoformat()

        data: ApiReturn[list[Packet]] = self._make_req(
            '/'.join(['devices', 'by-name', device_name, 'packets']),
            params=params,
            max_pages=max_pages,
        )
        return data

    def get_packets_by_folder(
            self,
            folder: str,
            *,
            packet_type: Literal['up', 'down'] | None = None,
            start: datetime | None = None,
            end: datetime | None = None,
            limit: Annotated[int, _ValueRange(1, 100)] = 100,
            max_pages: int | None = None,
    ) -> ApiReturn[list[Packet]]:
        """Get the original packets from the API. This is returned as the raw
        API-return-value. The sorting is fixed to ``transceived_at``.

        :param folder: The folder in the Elements IoT system to query for this
            this can be e.g. ``'stadt-dortmund-klimasensoren-inaktiv-sht35'``
        :param packet_type: Filter for packet_types (either ``up`` or ``down``)
            if ``None`` all package types are returned
        :param start: The datetime to start getting readings for. If ``None``,
            all available readings will be retrieved.
        :param end: The datetime to stop getting readings for. If ``None``,
            all available readings will be retrieved.
        :param limit: How many values to fetch per API request (must be between
            1 and 100).
        :param max_pages: After how many pages of pagination we stop, to avoid
            infinitely requesting data from the API.
        """
        params: dict[str, Any] = {'limit': limit}
        if packet_type:
            params['packet_type'] = packet_type
        if start:
            params['after'] = start.isoformat()
        if end:
            params['before'] = end.isoformat()

        data: ApiReturn[list[Packet]] = self._make_req(
            '/'.join(['tags', folder, 'packets']),
            params=params,
            max_pages=max_pages,
        )
        return data

    def __repr__(self) -> str:
        return (
            f'{type(self).__name__}'
            f'('
            f'api_location={self.api_location!r}, '
            f'api_key={(len(self.api_key) - 3) * "*"}{self.api_key[-3:]}'
            ')'
        )

    def __eq__(self, value: object) -> bool:
        if isinstance(value, type(self)):
            # we intentionally ignore the caches
            return (
                self.api_key == value.api_key and
                self.api_location == value.api_location
            )
        else:
            return False