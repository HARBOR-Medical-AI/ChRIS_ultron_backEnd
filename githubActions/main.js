const child_process = require('child_process');
const path = require('path');

const CONTAINER_ENGINE = process.env.INPUT_ENGINE;
const JUST_COMMAND = process.env.INPUT_COMMAND;

// On Ubuntu 26.04 the AppArmor profile for pasta does not let it receive a signal from
// Podman, so removing the last container on a rootless network fails with "rootless
// netns: kill network process: permission denied" (Debian bug 1100135): `just nuke` in
// the post step would fail after the tests have passed. Add the missing rule, if sudo
// needs no password; otherwise warn. (In the script, `\\n` reaches sed as `\n`.)
const PASTA_APPARMOR_FIX = `
profile=/etc/apparmor.d/usr.bin.pasta
if [ -f "$profile" ] && ! grep -q 'peer=podman' "$profile"; then
  if sudo -n true 2>/dev/null; then
    sudo sed -i 's|^}|  signal (receive) peer=podman,\\n}|' "$profile"
    sudo apparmor_parser -r "$profile"
    grep -q 'peer=podman' "$profile" \\
      || echo "::warning ::could not add 'signal (receive) peer=podman,' to $profile"
  else
    echo "::warning ::$profile lacks 'signal (receive) peer=podman,' and sudo needs a password: 'just nuke' may fail"
  fi
fi
`;

const script = `
set -x
${CONTAINER_ENGINE === 'podman' ? PASTA_APPARMOR_FIX : ''}
just prefer ${CONTAINER_ENGINE}

# start-up is being retried as a workaround for
# https://github.com/HARBOR-Medical-AI/ChRIS_ultron_backEnd/issues/573
for i in {1..5}; do
  just start-ancillary && start=good && break
  echo "::warning ::Ancillary services failed to start. Attempt=$i"
done

if [ "$start" != "good" ]; then
  echo "::error ::Failed to start ancillary services."
  exit 1
fi

just ${JUST_COMMAND}
rc=$?
if [ "$rc" != '0' ]; then
  just logs
fi
exit $rc
`;

child_process.execFileSync('bash', ['-c', script],
  {
    stdio: 'inherit',
    cwd: path.resolve(__dirname, '..')
  }
);

