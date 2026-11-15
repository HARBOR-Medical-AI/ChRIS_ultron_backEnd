STATUS_CHOICES = [("created", "Default initial"),
                  ("waiting", "Waiting to be scheduled"),
                  ("copying", "Copying files to compute env"),  # when supported
                  ("scheduled", "Scheduled on worker"),
                  ("started", "Started on compute env"),
                  ("uploading", "Uploading files from compute env"),  # when supported
                  ("registeringFiles", "Registering output files"),
                  ("finishedSuccessfully", "Finished successfully"),
                  ("finishedWithError", "Finished with error"),
                  ("cancelled", "Cancelled")]

STATUSES = frozenset(status for status, _ in STATUS_CHOICES)

ACTIVE_STATUSES = ['created', 'waiting', 'copying', 'scheduled', 'started', 'uploading',
                   'registeringFiles']

INACTIVE_STATUSES = ['finishedSuccessfully', 'finishedWithError', 'cancelled']

# the statuses in which an instance may have a job of its own in the remote compute
# (submitted, or being submitted), so that cancelling it needs a remote cleanup
REMOTE_JOB_STATUSES = ['copying', 'scheduled', 'started', 'uploading', 'registeringFiles']

REMOTE_CLEANUP_STATUS_CHOICES = [
    ('notStarted', 'Not started'),
    ('deletingData', 'Deleting remote data'),
    ('deletingContainers', 'Deleting remote containers'),
    ('complete', 'Cleanup complete'),
    ('failed', 'Cleanup failed'),
]

REMOTE_CLEANUP_STATUSES = frozenset(status for status, _ in REMOTE_CLEANUP_STATUS_CHOICES)

# what a transition to a terminal status writes beside it: the remote cleanup armed in the
# same UPDATE so that no crash between the two leaves it unscheduled (the periodic task
# handle_remote_cleanup drives every instance in 'deletingData')
ARM_REMOTE_CLEANUP = {'remote_cleanup_status': 'deletingData',
                      'remote_cleanup_retry_count': 0}
