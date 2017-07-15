#!/usr/bin/env python

from bson import ObjectId
import datetime
import dateutil.parser
from flask import Flask, request
from functools import wraps
import json
import os
import subprocess
import tempfile

from qlmdm import (
    top_dir,
    set_gpg,
    releases_dir,
    get_server_settings,
    get_db,
    get_logger,
)

log = get_logger(get_server_settings(), 'server')

os.chdir(top_dir)
set_gpg('server')

app = Flask(__name__)


def verify_signature():
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            try:
                try:
                    data = request.form['data']
                except:
                    raise Exception('Malformed request: no data')
                try:
                    signature = request.form['signature']
                except:
                    raise Exception('Malformed request: no signature')
                with tempfile.NamedTemporaryFile() as data_file, \
                        tempfile.NamedTemporaryFile() as signature_file:
                    data_file.write(data)
                    data_file.flush()
                    signature_file.write(signature)
                    signature_file.flush()
                    try:
                        subprocess.check_output(
                            ('gpg', '--quiet', '--batch', '--verify',
                             signature_file.name, data_file.name),
                            stderr=subprocess.STDOUT)
                    except:
                        raise Exception('Bad signature')
            except:
                log.exception()
                raise
            return f(*args, **kwargs)
        return wrapper
    return decorator


def strip_dates(d):
    for key in d.keys():
        if key.endswith('_at'):
            del d[key]
        elif isinstance(d[key], dict):
            strip_dates(d[key])


def key_name(key, prefix):
    return prefix + '.' + key if prefix else key


def short_value(value):
    value = str(value)
    return '...' if len(value) > 40 else value


def dict_changes(old, new, prefix=None, changes=None):
    if changes is None:
        changes = []
    keys = sorted(list(set(old.keys() + new.keys())))
    for key in keys:
        if key not in new:
            changes.append('deleted {} ({})'.format(
                key_name(key, prefix),
                short_value(old[key])))
        elif key not in old:
            changes.append('added {} ({})'.format(
                key_name(key, prefix),
                short_value(new[key])))
        elif type(old[key]) != type(new[key]):  # noqa
            changes.append('type change {} ({} -> {}, new value {})'.format(
                key_name(key, prefix),
                type(old[key]), type(new[key]),
                short_value(new[key])))
        elif isinstance(old[key], dict):
            dict_changes(old[key], new[key], key_name(key, prefix), changes)
        elif str(old[key]) != str(new[key]):
            changes.append('changed {} ({} -> {})'.format(
                key_name(key, prefix),
                short_value(old[key]), short_value(new[key])))
    return changes


@app.route('/qlmdm/v1/submit', methods=('POST',))
@verify_signature()
def submit():
    db = get_db()
    which = []
    now = datetime.datetime.utcnow()
    data = json.loads(request.form['data'])
    hostname = data['hostname']
    datetimeify(data)
    spec = {'hostname': hostname}
    update = {
        '$set': {
            'submitted_at': now,
            'hostname': hostname,
        }
    }
    if 'plugins' in data:
        update['$set']['plugins'] = data['plugins']
        which.append('plugins')
    if data.get('commands', {}):
        for name, output in data['commands'].items():
            output['submitted_at'] = now
            update['$set']['commands.{}'.format(name)] = output
        which.append('commands')
    if which:
        old = db['submissions'].find_one(spec)
        db['submissions'].update(spec, update, upsert=True)
        log.info('Successful submission of {} by {}',
                 ', '.join(which), hostname)
        new = db['submissions'].find_one(spec)
        if old:
            strip_dates(old)
            strip_dates(new)
            changes = dict_changes(old, new)
            if changes:
                log.info('Changes for {}: {}', hostname, ', '.join(changes))
        return('ok')
    else:
        log.error('Empty submission from {}', hostname)
        return('error')


@app.route('/qlmdm/v1/update', methods=('POST',))
@verify_signature()
def update():
    db = get_db()
    data = json.loads(request.form['data'])
    hostname = data['hostname']
    old_release = data['old_release']
    releases = sorted(r for r in os.listdir(releases_dir)
                      if r.endswith('.asc'))
    response_data = {}
    if len(releases) == 0:
        response_data['status'] = 'current'
    else:
        current_release_file = releases[-1]
        current_release_number = \
            int(current_release_file[0:current_release_file.index('.')])
        if old_release >= current_release_number:
            response_data['status'] = 'current'
            log.debug('{} is current ({})', hostname, current_release_number)
        else:
            log.info('Sending release {} to {} (currently at {})',
                     current_release_number, hostname, old_release)
            response_data['status'] = 'out-of-date'
            response_data['current_release'] = current_release_number
            response_data['update'] = open(
                os.path.join(releases_dir, current_release_file)).read()

    patches = [{'id': str(d['_id']), 'files': d['files']}
               for d in db['patches'].find({'pending_hosts': hostname},
                                           projection=['files'])]
    if patches:
        log.info('Sending patches {} ({}) to {}',
                 ', '.join(p['id'] for p in patches),
                 ', '.join(f['path'] for p in patches for f in p['files']),
                 hostname)
        response_data['patches'] = patches

    return json.dumps(response_data)


@app.route('/qlmdm/v1/acknowledge_patch', methods=('POST',))
@verify_signature()
def acknowledge_patch():
    db = get_db()
    data = json.loads(request.form['data'])
    _id = data['id']
    hostname = data['hostname']
    db['patches'].update(
        {'_id': ObjectId(_id)}, {'$push': {'completed_hosts': hostname},
                                 '$pull': {'pending_hosts': hostname}})
    log.info('{} acknowledged patch {}', hostname, _id)
    return 'ok'


def datetimeify(d):
    for key, value in d.items():
        if isinstance(value, dict):
            datetimeify(value)
        elif key.endswith('_at'):
            try:
                dt = dateutil.parser.parse(value)
                d[key] = dt
            except:
                pass


def main():
    global server_settings

    server_settings = get_server_settings()

    app.run(host='0.0.0.0', port=server_settings.get('port', 80),
            threaded=server_settings.get('threaded', True))


if __name__ == '__main__':
    main()
