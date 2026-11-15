
from .tasks import schedule_plugin_instance
from .models import PluginInstance, ACTIVE_STATUSES
from .enums import STATUSES


def run_if_ready(plg_inst, previous):
    """
    Set the status of ``plg_inst``, just created, accordingly depending on the status of
    its previous plugin instance. Plugin instances of type 'ts' also consider the status
    of each of its possibly multiple parents.
    """
    parent_ids = []
    if plg_inst.plugin.meta.type == 'ts':
        param = plg_inst.string_param.filter(plugin_param__name='plugininstances').first()
        if param and param.value:
            parent_ids = [int(parent_id) for parent_id in param.value.split(',')]

    if parent_ids:
        parents = PluginInstance.objects.filter(pk__in=parent_ids)
        all_parents_finished = True

        for parent in parents:
            if parent.status in ACTIVE_STATUSES:
                plg_inst.transition('waiting', 'created')
                all_parents_finished = False
                break
            if parent.status in ('finishedWithError', 'cancelled'):
                plg_inst.transition('cancelled', 'created')
                all_parents_finished = False
                break

        if all_parents_finished:
            schedule_plugin_instance(plg_inst, 'created')

    elif previous is None or previous.status == 'finishedSuccessfully':
        schedule_plugin_instance(plg_inst, 'created')

    elif previous.status in ACTIVE_STATUSES:
        plg_inst.transition('waiting', 'created')

    elif previous.status in ('finishedWithError', 'cancelled'):
        plg_inst.transition('cancelled', 'created')


def cancel_plugin_instances(plg_insts, source=None):
    """
    Cancel these plugin instances from the statuses ``source``, by default whatever their
    status but 'cancelled', each with its remote cleanup armed in the same UPDATE when it
    may have a job in the remote compute (``PluginInstance.cancel``). Returns how many it
    moved to 'cancelled'.
    """
    source = source or STATUSES - {'cancelled'}
    return sum(bool(inst.cancel(source)) for inst in plg_insts)


def cancel_plugin_instance_tree(plg_inst):
    """
    Cancel an active plugin instance and, once it is cancelled (by this call or before),
    every descendant whatever its status: their input can no longer be trusted. Returns
    the instance's ``StatusWrite``: lost with another status when it ended otherwise
    meanwhile, and then nothing changes.
    """
    cancelled = plg_inst.cancel()

    if not cancelled and cancelled.status != 'cancelled':
        return cancelled

    if not cancelled:  # cancelled before: what the response shows
        plg_inst.refresh_from_db(fields=['status'])

    cancel_plugin_instances([inst for inst in plg_inst.get_descendant_instances()
                             if inst.id != plg_inst.id])
    return cancelled
