"""Read LsDL JSON in memory and preserve existing MQTT sensor identities.

No archive members are extracted or executed. Migration fails closed if an
existing entity cannot be served from the gateway snapshot.
"""
import hashlib
import io
import json
import math
from pathlib import PurePosixPath
import re
import tarfile
from dataclasses import dataclass

MAX_FILE = 65536
MAX_FILES = 4096
# schema name: (discovery id, display name, device class, unit, state class)
SENSORS = {
    'soil_temperature': ('soil_temperature', 'Bodentemperatur', 'temperature', '°C', 'measurement'),
    'soil_moisture': ('soil_moisture', 'Bodenfeuchte', 'moisture', '%', 'measurement'),
    'battery_level': ('battery', 'Batterie', 'battery', '%', 'measurement'),
    'rf_link_quality': ('rf_link_quality', 'Funkqualität', None, '%', 'measurement'),
    'light': ('light', 'Helligkeit', 'illuminance', 'lx', 'measurement'),
    'status': ('mower_status', 'Mäherstatus', None, None, None),
    'error': ('error', 'Fehlercode', None, None, None),
    'running_time': ('running_time', 'Laufzeit', 'duration', 'h', 'total_increasing'),
    'cutting_time': ('cutting_time', 'Mähzeit', 'duration', 'h', 'total_increasing'),
    'measurement_interval': ('measurement_interval', 'Messintervall', 'duration', 's', None),
    'frost_warning': ('frost_warning', 'Frostwarnung', None, None, None),
}
ALIASES = {'temperature': 'soil_temperature', 'mower_status': 'status', 'battery': 'battery_level'}


@dataclass
class Device:
    serial: str
    values: dict

    @property
    def key(self):
        return hashlib.sha256(self.serial.encode()).hexdigest()[:12]


def numeric(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError('Invalid sensor value')
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('Invalid sensor value')
    return str(value)


def parse_snapshot(raw):
    documents = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:') as archive:
        for index, member in enumerate(archive):
            if index >= MAX_FILES:
                raise ValueError('Too many snapshot files')
            path = PurePosixPath(member.name)
            if path.is_absolute() or '..' in path.parts:
                raise ValueError('Unsafe snapshot path')
            if not member.isfile() or path.suffix != '.json':
                continue
            if member.size > MAX_FILE:
                raise ValueError('Snapshot file too large')
            data = json.loads(archive.extractfile(member).read())
            if not isinstance(data, dict):
                raise ValueError('Invalid LsDL document')
            documents[str(path)] = data
    devices = []
    for name, description in documents.items():
        path = PurePosixPath(name)
        if not path.name.startswith('Device_descriptionID_'):
            continue
        serial = description.get('serialid')
        if not isinstance(serial, str) or not serial or len(serial) > 256:
            continue
        values = {}
        for schema_path, schema in documents.items():
            candidate = PurePosixPath(schema_path)
            if candidate.parent != path.parent / 'Value_description':
                continue
            resource = schema.get('name')
            resource = ALIASES.get(resource, resource) if isinstance(resource, str) else None
            if resource not in SENSORS:
                continue
            resource_id = schema.get('id')
            if resource_id is None:
                match = re.search(r'(\d+)\.json$', candidate.name)
                resource_id = match.group(1) if match else None
            if not re.fullmatch(r'\d{1,10}', str(resource_id)):
                continue
            value = documents.get(str(path.parent / 'Value' / f'Value_{resource_id}r.json'))
            if value is not None:
                values[resource] = numeric(value.get('value'))
        if values:
            devices.append(Device(serial, values))
    if len({d.serial for d in devices}) != len(devices):
        raise ValueError('Duplicate gateway identity')
    if not devices:
        raise ValueError('No supported LsDL sensor values')
    return devices


def validate_config(topic, payload, prefix, ha_prefix):
    match = re.fullmatch(re.escape(ha_prefix) + r'/(sensor|binary_sensor)/gardena_([a-z0-9]+)/([a-z0-9_]+)/config', topic)
    if not match:
        return None
    config = json.loads(payload)
    if not isinstance(config, dict):
        raise ValueError('Invalid existing discovery')
    key = match[2]
    device = config.get('device', {})
    if not isinstance(device, dict) or device.get('manufacturer') != 'GARDENA':
        return None
    if device.get('identifiers') != ['gardena_' + key]:
        raise ValueError('Ambiguous existing identity')
    state = config.get('state_topic', '')
    resource = re.fullmatch(re.escape(prefix + '/' + key + '/') + r'([a-z0-9_]+)/state', state)
    if not resource or not isinstance(config.get('unique_id'), str):
        raise ValueError('Unsupported existing sensor topic')
    schema = ALIASES.get(resource[1], resource[1])
    if schema not in SENSORS:
        raise ValueError('Unsupported existing sensor resource')
    # Keep entity metadata and templates, but never import arbitrary routing fields.
    allowed = {'unique_id', 'object_id', 'name', 'state_topic', 'device', 'device_class',
        'unit_of_measurement', 'state_class', 'icon', 'entity_category', 'value_template',
        'payload_on', 'payload_off', 'enabled_by_default'}
    return {'topic': topic, 'original': config, 'config': {k: v for k, v in config.items() if k in allowed},
            'key': key, 'resource': schema}


def make_plan(devices, existing, prefix, ha_prefix, mowers):
    """All existing sensor records must map; no silent partial takeover.

    First migration supports one LsDL device and one mower. Fresh sensor-only
    gateways support multiple devices. Persisted plans avoid re-association.
    """
    keys = {record['key'] for record in existing}
    if existing and (len(keys) != 1 or len(devices) != 1 or len(mowers) != 1):
        raise ValueError('Migration requires exactly one device and one mower')
    if mowers and (len(mowers) != 1 or len(devices) != 1):
        raise ValueError('Mower sensor association is ambiguous')
    records = []
    for device in devices:
        key = next(iter(keys)) if existing else device.key
        if existing:
            for record in existing:
                if record['resource'] not in device.values:
                    raise ValueError('Existing sensor has no current gateway value')
                records.append({**record, 'serial': device.serial})
        else:
            for resource in device.values:
                object_id, label, device_class, unit, state_class = SENSORS[resource]
                config = {'unique_id': f'gardena_{key}_{object_id}', 'name': label,
                    'state_topic': f'{prefix}/{key}/{resource}/state',
                    'device': {'identifiers': ['gardena_' + key], 'name': 'GARDENA Local',
                               'manufacturer': 'GARDENA'}}
                # Units are schema-dependent; use only unambiguous measurements.
                if resource in ('soil_temperature', 'soil_moisture', 'battery_level'):
                    config.update(device_class=device_class, unit_of_measurement=unit, state_class=state_class)
                records.append({'topic': f'{ha_prefix}/sensor/gardena_{key}/{object_id}/config',
                    'config': config, 'key': key, 'resource': resource, 'serial': device.serial})
    if mowers:
        mowers[0].publisher_key = records[0]['key']
        mowers[0].publisher_identifier = 'gardena_' + records[0]['key']
    return records


def readings(plan, devices):
    current = {d.serial: d.values for d in devices}
    result = []
    for record in plan:
        value = current.get(record['serial'], {}).get(record['resource'])
        if value is None:
            raise ValueError('Sensor disappeared from gateway snapshot')
        result.append((record['config']['state_topic'], value))
    return result


def publish(mqtt, plan, devices, availability):
    values = readings(plan, devices)  # Validate every value before first write.
    for record in plan:
        config = dict(record['config'], availability_topic=availability,
                      payload_available='online', payload_not_available='offline')
        mqtt.publish(record['topic'], json.dumps(config, separators=(',', ':')), retain=True)
    for topic, value in values:
        mqtt.publish(topic, value, retain=True)
