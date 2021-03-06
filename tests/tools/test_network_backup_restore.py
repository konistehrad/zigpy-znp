import json

import pytest
from jsonschema import ValidationError

import zigpy_znp.types as t
import zigpy_znp.config as conf
from zigpy_znp.api import ZNP
from zigpy_znp.znp import security
from zigpy_znp.znp.utils import NetworkInfo
from zigpy_znp.types.nvids import ExNvIds, OsalNvIds
from zigpy_znp.tools.common import validate_backup_json
from zigpy_znp.tools.energy_scan import channels_from_channel_mask
from zigpy_znp.zigbee.application import ControllerApplication
from zigpy_znp.tools.network_backup import main as network_backup
from zigpy_znp.tools.network_restore import main as network_restore

from ..conftest import (
    ALL_DEVICES,
    EMPTY_DEVICES,
    FORMED_DEVICES,
    CoroutineMock,
    BaseZStack1CC2531,
    BaseZStack3CC2531,
    BaseLaunchpadCC26X2R1,
)

BARE_NETWORK_INFO = NetworkInfo(
    extended_pan_id=t.EUI64.convert("ab:de:fa:bc:de:fa:bc:de"),
    ieee=None,
    nwk=None,
    channel=None,
    channels=None,
    pan_id=None,
    nwk_update_id=None,
    security_level=None,
    network_key=None,
    network_key_seq=None,
)


@pytest.fixture
def backup_json():
    return {
        "metadata": {
            "format": "zigpy/open-coordinator-backup",
            "internal": {
                "creation_time": "2021-02-16T22:29:28+00:00",
                "zstack": {"version": 3.3},
            },
            "source": "zigpy-znp@0.3.0",
            "version": 1,
        },
        "stack_specific": {"zstack": {"tclk_seed": "c04884427c8a1ed7bb8412815ccce7aa"}},
        "channel": 25,
        "channel_mask": [15, 20, 25],
        "pan_id": "feed",
        "extended_pan_id": "abdefabcdefabcde",
        "coordinator_ieee": "0123456780123456",
        "nwk_update_id": 2,
        "security_level": 5,
        "network_key": {
            "frame_counter": 66781,
            "key": "37668fd64e35e03342e5ef9f35ccf4ab",
            "sequence_number": 1,
        },
        "devices": [
            {"ieee_address": "000b57fffe36b9a0", "nwk_address": "f319"},  # No key
            {
                "ieee_address": "000b57fffe38b212",
                "link_key": {
                    "key": "d2fabcbc83dd15d7a9362a7fa39becaa",  # Derived from seed
                    "rx_counter": 123,
                    "tx_counter": 456,
                },
                "nwk_address": "9672",
            },
            {
                "ieee_address": "aabbccddeeff0011",
                "link_key": {
                    "key": "01234567801234567801234567801234",  # Not derived from seed
                    "rx_counter": 112233,
                    "tx_counter": 445566,
                },
                "nwk_address": "abcd",
            },
        ],
    }


def test_schema_validation(backup_json):
    validate_backup_json(backup_json)


def test_schema_validation_counters(backup_json):
    backup_json["devices"][1]["link_key"]["tx_counter"] = 0xFFFFFFFF
    validate_backup_json(backup_json)
    backup_json["devices"][1]["link_key"]["tx_counter"] = 0xFFFFFFFF + 1

    with pytest.raises(ValidationError):
        validate_backup_json(backup_json)


def test_schema_validation_device_key_info(backup_json):
    validate_backup_json(backup_json)
    backup_json["devices"][1]["link_key"]["key"] = None

    with pytest.raises(ValidationError):
        validate_backup_json(backup_json)


@pytest.mark.parametrize("device", FORMED_DEVICES)
@pytest.mark.asyncio
async def test_network_backup_formed(device, make_znp_server, tmp_path):
    znp_server = make_znp_server(server_cls=device)

    backup_file = tmp_path / "backup.json"
    await network_backup([znp_server._port_path, "-o", str(backup_file)])

    backup = json.loads(backup_file.read_text())

    # XXX: actually test that the values match up with what the device NVRAM contains
    assert backup["metadata"]["version"] == 1
    assert backup["metadata"]["format"] == "zigpy/open-coordinator-backup"
    assert backup["metadata"]["source"].startswith("zigpy-znp@")

    assert len(bytes.fromhex(backup["coordinator_ieee"])) == 8
    assert len(bytes.fromhex(backup["pan_id"])) == 2
    assert len(bytes.fromhex(backup["extended_pan_id"])) == 8
    assert 0 <= backup["nwk_update_id"] <= 0xFF
    assert 0 <= backup["security_level"] <= 7
    assert backup["channel"] in list(range(11, 26 + 1))

    channel_mask = t.Channels.from_channel_list(backup["channel_mask"])
    assert backup["channel"] in channels_from_channel_mask(channel_mask)

    assert len(bytes.fromhex(backup["network_key"]["key"])) == 16
    assert 0x00 <= backup["network_key"]["sequence_number"] <= 0xFF
    assert 0x00000000 <= backup["network_key"]["frame_counter"] <= 0xFFFFFFFF

    assert isinstance(backup["devices"], list)


@pytest.mark.parametrize("device", EMPTY_DEVICES)
@pytest.mark.asyncio
async def test_network_backup_empty(device, make_znp_server):
    znp_server = make_znp_server(server_cls=device)

    with pytest.raises(RuntimeError):
        await network_backup([znp_server._port_path, "-o", "-"])


@pytest.mark.parametrize("device", ALL_DEVICES)
@pytest.mark.asyncio
async def test_network_restore(device, make_znp_server, backup_json, tmp_path, mocker):
    backup_file = tmp_path / "backup.json"
    backup_file.write_text(json.dumps(backup_json))

    znp_server = make_znp_server(server_cls=device)

    async def mock_startup(self, *, force_form):
        assert force_form

        config = self.config[conf.CONF_NWK]

        assert config[conf.CONF_NWK_KEY] == t.KeyData(
            bytes.fromhex("37668fd64e35e03342e5ef9f35ccf4ab")
        )
        assert config[conf.CONF_NWK_PAN_ID] == 0xFEED
        assert config[conf.CONF_NWK_CHANNEL] == 25
        assert config[conf.CONF_NWK_EXTENDED_PAN_ID] == t.EUI64.convert(
            "ab:de:fa:bc:de:fa:bc:de"
        )

        znp = ZNP(self.config)
        await znp.connect()

        if OsalNvIds.APS_LINK_KEY_TABLE not in znp_server._nvram[ExNvIds.LEGACY]:
            znp_server._nvram[ExNvIds.LEGACY][OsalNvIds.APS_LINK_KEY_TABLE] = (
                b"\x00" * 1000
            )

        if OsalNvIds.NIB not in znp_server._nvram[ExNvIds.LEGACY]:
            znp_server._nvram[ExNvIds.LEGACY][
                OsalNvIds.NIB
            ] = znp_server.nvram_serialize(znp_server._default_nib())

        self._znp = znp
        self._znp.set_application(self)

        self._bind_callbacks()

    startup_mock = mocker.patch.object(
        ControllerApplication, "startup", side_effect=mock_startup, autospec=True
    )

    load_nwk_info_mock = mocker.patch(
        "zigpy_znp.api.load_network_info",
        new=CoroutineMock(return_value=BARE_NETWORK_INFO),
    )

    write_tc_counter_mock = mocker.patch(
        "zigpy_znp.tools.network_restore.write_tc_frame_counter", new=CoroutineMock()
    )
    write_devices_mock = mocker.patch(
        "zigpy_znp.tools.network_restore.write_devices", new=CoroutineMock()
    )

    # Perform the "restore"
    await network_restore([znp_server._port_path, "-i", str(backup_file), "-c", "2500"])

    # The NIB should contain correct values
    nib = znp_server.nvram_deserialize(
        znp_server._nvram[ExNvIds.LEGACY][OsalNvIds.NIB], t.NIB
    )
    assert nib.channelList == t.Channels.from_channel_list([15, 20, 25])
    assert nib.nwkUpdateId == 2
    assert nib.SecurityLevel == 5

    # And validate that the low-level functions were called appropriately
    assert startup_mock.call_count == 1
    assert startup_mock.mock_calls[0][2]["force_form"] is True

    assert load_nwk_info_mock.call_count == 1

    assert write_tc_counter_mock.call_count == 1
    assert write_tc_counter_mock.mock_calls[0][1][1] == 66781 + 2500

    assert write_devices_mock.call_count == 1
    write_devices_call = write_devices_mock.mock_calls[0]

    assert write_devices_call[2]["counter_increment"] == 2500

    if issubclass(device, BaseZStack1CC2531):
        assert write_devices_call[2]["seed"] is None
    else:
        assert write_devices_call[2]["seed"] == bytes.fromhex(
            "c04884427c8a1ed7bb8412815ccce7aa"
        )

    assert sorted(write_devices_call[1][1], key=lambda d: d.nwk) == [
        security.StoredDevice(
            ieee=t.EUI64.convert("00:0b:57:ff:fe:38:b2:12"),
            nwk=0x9672,
            aps_link_key=t.KeyData.deserialize(
                bytes.fromhex("d2fabcbc83dd15d7a9362a7fa39becaa")
            )[0],
            rx_counter=123,
            tx_counter=456,
        ),
        security.StoredDevice(
            ieee=t.EUI64.convert("aa:bb:cc:dd:ee:ff:00:11"),
            nwk=0xABCD,
            aps_link_key=t.KeyData.deserialize(
                bytes.fromhex("01234567801234567801234567801234")
            )[0],
            rx_counter=112233,
            tx_counter=445566,
        ),
        security.StoredDevice(
            ieee=t.EUI64.convert("00:0b:57:ff:fe:36:b9:a0"),
            nwk=0xF319,
        ),
    ]


@pytest.mark.asyncio
async def test_tc_frame_counter_zstack1(make_connected_znp):
    znp, znp_server = await make_connected_znp(BaseZStack1CC2531)
    znp_server._nvram[ExNvIds.LEGACY] = {
        OsalNvIds.NWKKEY: b"\x01" + b"\xAB" * 16 + b"\x78\x56\x34\x12"
    }

    assert (await security.read_tc_frame_counter(znp)) == 0x12345678

    await security.write_tc_frame_counter(znp, 0xAABBCCDD)
    assert (await security.read_tc_frame_counter(znp)) == 0xAABBCCDD


@pytest.mark.asyncio
async def test_tc_frame_counter_zstack30(make_connected_znp):
    znp, znp_server = await make_connected_znp(BaseZStack3CC2531)
    znp.network_info = BARE_NETWORK_INFO
    znp_server._nvram[ExNvIds.LEGACY] = {
        # This value is ignored
        OsalNvIds.NWKKEY: b"\x01" + b"\xAB" * 16 + b"\x78\x56\x34\x12",
        # Wrong EPID, ignored
        OsalNvIds.LEGACY_NWK_SEC_MATERIAL_TABLE_START: bytes.fromhex(
            "0f000000058eea0f004b1200"
        ),
        # Exact EPID match, used
        (OsalNvIds.LEGACY_NWK_SEC_MATERIAL_TABLE_START + 1): bytes.fromhex("01000000")
        + BARE_NETWORK_INFO.extended_pan_id.serialize(),
        # Generic EPID but ignored since EPID matches
        (OsalNvIds.LEGACY_NWK_SEC_MATERIAL_TABLE_START + 2): bytes.fromhex("02000000")
        + b"\xFF" * 8,
    }

    assert (await security.read_tc_frame_counter(znp)) == 0x00000001

    # If we change the EPID, the generic entry will be used
    old_nwk_info = znp.network_info
    znp.network_info = znp.network_info.replace(
        extended_pan_id=t.EUI64.convert("11:22:33:44:55:66:77:88")
    )
    assert (await security.read_tc_frame_counter(znp)) == 0x00000002

    # Changing the frame counter will always change the global entry in this case
    await security.write_tc_frame_counter(znp, 0xAABBCCDD)
    assert (await security.read_tc_frame_counter(znp)) == 0xAABBCCDD
    assert znp_server._nvram[ExNvIds.LEGACY][
        OsalNvIds.LEGACY_NWK_SEC_MATERIAL_TABLE_START + 2
    ].startswith(t.uint32_t(0xAABBCCDD).serialize())

    # Global entry is ignored if the EPID matches
    znp.network_info = old_nwk_info
    assert (await security.read_tc_frame_counter(znp)) == 0x00000001
    await security.write_tc_frame_counter(znp, 0xABCDABCD)
    assert znp_server._nvram[ExNvIds.LEGACY][
        OsalNvIds.LEGACY_NWK_SEC_MATERIAL_TABLE_START + 1
    ].startswith(t.uint32_t(0xABCDABCD).serialize())


@pytest.mark.asyncio
async def test_tc_frame_counter_zstack33(make_connected_znp):
    znp, znp_server = await make_connected_znp(BaseLaunchpadCC26X2R1)
    znp.network_info = BARE_NETWORK_INFO
    znp_server._nvram = {
        ExNvIds.LEGACY: {
            # This value is ignored
            OsalNvIds.NWKKEY: bytes.fromhex(
                "00c927e9ce1544c9aa42340e4d5dc4c257e4010001000000"
            )
        },
        ExNvIds.NWK_SEC_MATERIAL_TABLE: {
            # Wrong EPID, ignored
            0x0000: bytes.fromhex("0100000037a7479777d7a224"),
            # Right EPID, used
            0x0001: bytes.fromhex("02000000")
            + BARE_NETWORK_INFO.extended_pan_id.serialize(),
        },
    }

    assert (await security.read_tc_frame_counter(znp)) == 0x00000002

    # If we change the EPID, the generic entry will be used. It doesn't exist.
    old_nwk_info = znp.network_info
    znp.network_info = znp.network_info.replace(
        extended_pan_id=t.EUI64.convert("11:22:33:44:55:66:77:88")
    )

    with pytest.raises(ValueError):
        await security.read_tc_frame_counter(znp)

    # The correct entry will be updated
    znp.network_info = old_nwk_info
    assert (await security.read_tc_frame_counter(znp)) == 0x00000002
    await security.write_tc_frame_counter(znp, 0xABCDABCD)
    assert znp_server._nvram[ExNvIds.NWK_SEC_MATERIAL_TABLE][0x0001].startswith(
        t.uint32_t(0xABCDABCD).serialize()
    )
