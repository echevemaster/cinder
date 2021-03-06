# Copyright (c) 2012 OpenStack Foundation
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""Volume-related Utilities and helpers."""


import math

from oslo.config import cfg

from cinder.brick.local_dev import lvm as brick_lvm
from cinder import exception
from cinder.openstack.common import log as logging
from cinder.openstack.common.notifier import api as notifier_api
from cinder.openstack.common import processutils
from cinder.openstack.common import strutils
from cinder.openstack.common import timeutils
from cinder import units
from cinder import utils


CONF = cfg.CONF

LOG = logging.getLogger(__name__)


def get_host_from_queue(queuename):
    # This assumes the queue is named something like cinder-volume
    # and does not have dot separators in the queue name
    return queuename.split('@', 1)[0].split('.', 1)[1]


def notify_usage_exists(context, volume_ref, current_period=False):
    """Generates 'exists' notification for a volume for usage auditing
       purposes.

       Generates usage for last completed period, unless 'current_period'
       is True.
    """
    begin, end = utils.last_completed_audit_period()
    if current_period:
        audit_start = end
        audit_end = timeutils.utcnow()
    else:
        audit_start = begin
        audit_end = end

    extra_usage_info = dict(audit_period_beginning=str(audit_start),
                            audit_period_ending=str(audit_end))

    notify_about_volume_usage(context, volume_ref,
                              'exists', extra_usage_info=extra_usage_info)


def null_safe_str(s):
    return str(s) if s else ''


def _usage_from_volume(context, volume_ref, **kw):
    usage_info = dict(tenant_id=volume_ref['project_id'],
                      user_id=volume_ref['user_id'],
                      availability_zone=volume_ref['availability_zone'],
                      volume_id=volume_ref['id'],
                      volume_type=volume_ref['volume_type_id'],
                      display_name=volume_ref['display_name'],
                      launched_at=null_safe_str(volume_ref['launched_at']),
                      created_at=null_safe_str(volume_ref['created_at']),
                      status=volume_ref['status'],
                      snapshot_id=volume_ref['snapshot_id'],
                      size=volume_ref['size'])

    usage_info.update(kw)
    return usage_info


def notify_about_volume_usage(context, volume, event_suffix,
                              extra_usage_info=None, host=None):
    if not host:
        host = CONF.host

    if not extra_usage_info:
        extra_usage_info = {}

    usage_info = _usage_from_volume(context, volume, **extra_usage_info)

    notifier_api.notify(context, 'volume.%s' % host,
                        'volume.%s' % event_suffix,
                        notifier_api.INFO, usage_info)


def _usage_from_snapshot(context, snapshot_ref, **extra_usage_info):
    usage_info = {
        'tenant_id': snapshot_ref['project_id'],
        'user_id': snapshot_ref['user_id'],
        'availability_zone': snapshot_ref.volume['availability_zone'],
        'volume_id': snapshot_ref['volume_id'],
        'volume_size': snapshot_ref['volume_size'],
        'snapshot_id': snapshot_ref['id'],
        'display_name': snapshot_ref['display_name'],
        'created_at': str(snapshot_ref['created_at']),
        'status': snapshot_ref['status'],
        'deleted': null_safe_str(snapshot_ref['deleted'])
    }

    usage_info.update(extra_usage_info)
    return usage_info


def notify_about_snapshot_usage(context, snapshot, event_suffix,
                                extra_usage_info=None, host=None):
    if not host:
        host = CONF.host

    if not extra_usage_info:
        extra_usage_info = {}

    usage_info = _usage_from_snapshot(context, snapshot, **extra_usage_info)

    notifier_api.notify(context, 'snapshot.%s' % host,
                        'snapshot.%s' % event_suffix,
                        notifier_api.INFO, usage_info)


def _calculate_count(size_in_m, blocksize):

    # Check if volume_dd_blocksize is valid
    try:
        # Rule out zero-sized/negative dd blocksize which
        # cannot be caught by strutils
        if blocksize.startswith(('-', '0')):
            raise ValueError
        bs = strutils.to_bytes(blocksize)
    except (ValueError, TypeError):
        msg = (_("Incorrect value error: %(blocksize)s, "
                 "it may indicate that \'volume_dd_blocksize\' "
                 "was configured incorrectly. Fall back to default.")
               % {'blocksize': blocksize})
        LOG.warn(msg)
        # Fall back to default blocksize
        CONF.clear_override('volume_dd_blocksize')
        blocksize = CONF.volume_dd_blocksize
        bs = strutils.to_bytes(blocksize)

    count = math.ceil(size_in_m * units.MiB / float(bs))

    return blocksize, int(count)


def copy_volume(srcstr, deststr, size_in_m, blocksize, sync=False,
                execute=utils.execute):
    # Use O_DIRECT to avoid thrashing the system buffer cache
    extra_flags = ['iflag=direct', 'oflag=direct']

    # Check whether O_DIRECT is supported
    try:
        execute('dd', 'count=0', 'if=%s' % srcstr, 'of=%s' % deststr,
                *extra_flags, run_as_root=True)
    except processutils.ProcessExecutionError:
        extra_flags = []

    # If the volume is being unprovisioned then
    # request the data is persisted before returning,
    # so that it's not discarded from the cache.
    if sync and not extra_flags:
        extra_flags.append('conv=fdatasync')

    blocksize, count = _calculate_count(size_in_m, blocksize)

    # Perform the copy
    execute('dd', 'if=%s' % srcstr, 'of=%s' % deststr,
            'count=%d' % count,
            'bs=%s' % blocksize,
            *extra_flags, run_as_root=True)


def clear_volume(volume_size, volume_path, volume_clear=None,
                 volume_clear_size=None):
    """Unprovision old volumes to prevent data leaking between users."""
    if volume_clear is None:
        volume_clear = CONF.volume_clear

    if volume_clear_size is None:
        volume_clear_size = CONF.volume_clear_size

    if volume_clear_size == 0:
        volume_clear_size = volume_size

    LOG.info(_("Performing secure delete on volume: %s") % volume_path)

    if volume_clear == 'zero':
        return copy_volume('/dev/zero', volume_path, volume_clear_size,
                           CONF.volume_dd_blocksize,
                           sync=True, execute=utils.execute)
    elif volume_clear == 'shred':
        clear_cmd = ['shred', '-n3']
        if volume_clear_size:
            clear_cmd.append('-s%dMiB' % volume_clear_size)
    else:
        raise exception.InvalidConfigurationValue(
            option='volume_clear',
            value=volume_clear)

    clear_cmd.append(volume_path)
    utils.execute(*clear_cmd, run_as_root=True)


def supports_thin_provisioning():
    return brick_lvm.LVM.supports_thin_provisioning(
        utils.get_root_helper())


def get_all_volumes(vg_name=None):
    return brick_lvm.LVM.get_all_volumes(
        utils.get_root_helper(),
        vg_name)


def get_all_physical_volumes(vg_name=None):
    return brick_lvm.LVM.get_all_physical_volumes(
        utils.get_root_helper(),
        vg_name)


def get_all_volume_groups(vg_name=None):
    return brick_lvm.LVM.get_all_volume_groups(
        utils.get_root_helper(),
        vg_name)
