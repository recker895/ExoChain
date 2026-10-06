# GitHub Actions → existing EC2

The pipeline is created locally, **not activated**. It leaves the existing domain,
Nginx, HTTPS certificate, configuration and run history in place. No AWS resources
are created and no Git commits are pushed by these files.

## Pipeline

Pull requests and `main` run dependency checks, hashed Python installation,
production npm audits, backend tests with isolated Redis integration, frontend
tests, TypeScript checks and a Linux standalone build. Only tested `main` commits
can deploy; superseded commits are rejected. GitHub builds the frontend, not EC2.

EC2 prepares a separate release and Python environment before stopping services.
It then briefly stops both services, switches a `current` symlink, starts them and
checks `/livez`, runs API, frontend HTML, public HTTPS and an exact release marker.
Failure restores and checks the previous code. `/readyz` is not a gate because
optional providers may be unavailable. This is not a claim that all providers work.

A transient systemd unit survives an SSH disconnect; a server lock prevents
overlapping switches. Each release shares the original `.env` and `data` paths.
Rollback changes code, **not** database migrations or external actions.

## 1. Synchronize the server security updates

The local repository still pins Next.js 16.3.5; the existing server was patched to
16.3.6. CI deliberately fails until the matching manifest and lockfile are saved
in Git. Do not invent lockfile integrity hashes.

In **Windows PowerShell**:

```powershell
scp -i "C:\Users\Recker\Downloads\exochain-key.pem" ubuntu@15.206.189.30:/home/ubuntu/ExoChain/dashboard/frontend/package.json "G:\Projects\ExoChain-Engine\dashboard\frontend\package.json"
scp -i "C:\Users\Recker\Downloads\exochain-key.pem" ubuntu@15.206.189.30:/home/ubuntu/ExoChain/dashboard/frontend/package-lock.json "G:\Projects\ExoChain-Engine\dashboard\frontend\package-lock.json"
cd "G:\Projects\ExoChain-Engine"
python deploy/ec2/check_dependencies.py
git diff -- dashboard/frontend/package.json
```

Review both files before committing. Alternatively regenerate the lockfile using
npm and the patched exact versions when registry access is available. The version
floor is not a complete security guarantee: npm audits must also pass.

## 2. One-time EC2 preparation

First check the current services work and there is at least 2 GiB free disk space.
Copy the reviewed helpers from **Windows PowerShell**:

```powershell
scp -i "C:\Users\Recker\Downloads\exochain-key.pem" "G:\Projects\ExoChain-Engine\deploy\ec2\bootstrap.py" "G:\Projects\ExoChain-Engine\deploy\ec2\deploy_release.py" ubuntu@15.206.189.30:/home/ubuntu/
ssh -i "C:\Users\Recker\Downloads\exochain-key.pem" ubuntu@15.206.189.30
```

Then **inside Ubuntu SSH**:

```bash
python3 /home/ubuntu/bootstrap.py --node /home/ubuntu/.nvm/versions/node/v22.23.3/bin/node
systemctl is-active exochain-frontend exochain-backend nginx
cat /etc/ssh/ssh_host_ed25519_key.pub
```

Bootstrap adds systemd drop-ins and reloads systemd without restarting applications.
`/home/ubuntu/exochain-deploy/current` initially points to the original checkout.
Unit snapshots are saved in `unit-backups`. On an error, review partial setup; do
not repeatedly rerun bootstrap or delete the original app. Helper updates require
an explicit reviewed installation, not an application PR.

The printed SSH **public host key** is safe to put in GitHub. Never share `.env`,
private SSH keys or the TLS private key.

Create a separate CI SSH key on your own computer (not the personal PEM):

```powershell
ssh-keygen -t ed25519 -f "$env:USERPROFILE\.ssh\exochain-actions" -C "exochain-github-actions"
```

Use an empty passphrase for this unattended key. Add only the `.pub` line to the
server's `/home/ubuntu/.ssh/authorized_keys`, prefixed with
`no-agent-forwarding,no-port-forwarding,no-X11-forwarding,no-pty `, including the
space before `ssh-ed25519`. Normal personal SSH access stays unchanged. The CI
key has the existing Ubuntu user's deployment privileges: protect it as an
administrative credential. Never commit it. A future dedicated restricted deploy
user would further reduce privilege.

## 3. GitHub and AWS setup

Create GitHub repository environment **production**. Restrict its deployment
branches to **main only**, not all branches. Protect `main` and require **Test and
build** before merging. Optional required reviewers make deployment approval-gated.

Configure AWS IAM's GitHub OIDC provider, if absent:

- Provider URL: `https://token.actions.githubusercontent.com`
- Audience: `sts.amazonaws.com`

Create role `ExoChainGitHubDeploy` with the reviewed
[trust policy](aws-trust-policy.json) and [permissions policy](aws-permissions-policy.json).
Verify the supplied account, Mumbai region, repository and security group first.
Environment-based trust requires the `main` environment branch restriction above.
The role can modify ingress on this one security group and read rule metadata;
it needs no EC2 start/stop, S3, DNS, IAM administration or permanent AWS access keys.
Only trusted main code should get this role.

Set **repository Actions variable** `DEPLOY_ENABLED=false` until setup and CI pass.
This variable must be repository-level: the deployment job's condition is evaluated
before environment-level variables are loaded.

Set these **production environment variables**:

| Variable | Value |
| --- | --- |
| `EC2_HOST` | `15.206.189.30` (update if the IP changes) |
| `EC2_SECURITY_GROUP` | `sg-0aec07b18a7325416` |
| `AWS_DEPLOY_ROLE_ARN` | `arn:aws:iam::594559485041:role/ExoChainGitHubDeploy` |

Set these **production environment secrets**:

| Secret | Value |
| --- | --- |
| `EC2_SSH_PRIVATE_KEY` | Complete contents of the dedicated CI private key |
| `EC2_SSH_HOST_KEY` | `ssh-ed25519 AAAA...` from EC2's public host key, without final comment |

GitHub briefly allows SSH22 from its own runner IP **/32 only**, then removes only
the rule bearing that run's description. Keep personal SSH restricted to your IP,
ports80/443 public, and ports3000/8003 closed. Strict pinned SSH host verification
is required; no trust-on-first-use or `ssh-keyscan` bypass. If a runner is forcibly
killed, inspect/remove only stale `exochain-ci-<run-id>` rules; cleanup is best-effort
in that situation. Never remove the user's personal SSH rule.

## 4. Activate

Review, commit and push these new files and synchronized dependency files. No
secrets are needed for CI. Wait for **Test and build** to pass with deployment
disabled. After all server/IAM/GitHub settings are ready, change the repository
variable to `DEPLOY_ENABLED=true` and run **Test and deploy ExoChain** from Actions,
selecting branch **main**. It deploys that run's tested artifact, never a PR artifact.

Do not deploy arbitrary PR code, overwrite the working original checkout, or
bypass failing tests. Actions usage can incur charges depending on repository
visibility and account limits; no pricing is assumed here.

## Operations

- Updates: merge reviewed changes to `main`; test/build then deploy.
- Pause future deployments: `DEPLOY_ENABLED=false`; this does not interrupt a
  deployment already running on the server.
- An Elastic IP has not been confirmed. EC2 stop/start can change the public IP;
  update DNS and `EC2_HOST`, and verify server identity before the next deployment.
- Releases and uploaded archives are retained; no automatic deletion. With a 20 GiB
  disk, periodically review size/free space. Retain `current`, `previous`, and the
  original checkout. Remove only explicitly reviewed old generated releases and
  archives, never shared data. Failed preparation can leave a directory for review.
- Database migrations need backups and backwards compatibility: code rollback
  does not restore old database schemas, user runs or external API actions.
- Custom assets outside the original `.env` and `data` are not automatically shared.
  Use deliberate absolute paths or extend preparation before deploying them.
- The existing application environment mode, provider configuration and open-access
  policy are preserved, not silently changed by this workflow.
- Inspect GitHub logs and `journalctl -u exochain-backend -u exochain-frontend -n 100 --no-pager`.
  A deployment's transient unit
  is `exochain-deploy-<run-id>-<attempt>` while it exists.
- If both deployment and rollback fail, inspect logs before another attempt. Manual
  recovery: stop both services, point `current` at the reviewed `previous` target,
  start both, and verify HTTPS/API. Never use an untrusted target.
- Certbot's scheduled renewal is separate and unchanged.

Local tests do not prove GitHub/Linux builds or EC2 deployment. Verify those with
an actual successful Actions run before calling the pipeline operational.

Implementation checks on 2026-10-07: 31 deployment safety tests passed on Windows;
one actual Linux symlink test was skipped there. All 26 frontend tests, TypeScript
checking, helper lint and Python syntax checks passed. The full backend suite could
not complete: the Windows sandbox stalled creating the test client's loopback
socket pair. No application workaround was added. The dependency baseline gate
correctly rejects the repository's older Next.js pin until step 1 is completed.
