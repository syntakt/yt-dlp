"""Exercise certificate commands with synthetic state, without Docker or ACME."""

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def executable(path, text):
    path.write_text(text)
    path.chmod(0o755)


def test_host_renewal_explains_container_environment(tmp_path):
    result = subprocess.run(['sh', str(ROOT / 'nginx/renew-certificate.sh')],
                            env={'PATH': '/usr/bin:/bin'}, capture_output=True, text=True)
    assert result.returncode == 1
    assert 'docker exec nginx-ssl /renew-certificate.sh' in result.stderr
    assert './deploy.sh renew-cert' in result.stderr


def test_renewal_timeout_keeps_existing_certificate(tmp_path):
    binaries = tmp_path / 'bin'
    binaries.mkdir()
    executable(binaries / 'timeout', '#!/bin/sh\nexit 124\n')
    result = subprocess.run(['sh', str(ROOT / 'nginx/renew-certificate.sh')],
                            env={'PATH': f'{binaries}:/usr/bin:/bin', 'SSLIP_DOMAIN': 'downloads.example.org'},
                            capture_output=True, text=True)
    assert result.returncode == 124
    assert 'действующий сертификат не заменён' in result.stderr


@pytest.mark.parametrize('args', [[], ['--dry-run']])
@pytest.mark.parametrize('docker_status', [0, 17])
def test_deploy_renew_uses_running_container_without_host_env(tmp_path, args, docker_status):
    shutil.copyfile(ROOT / 'deploy.sh', tmp_path / 'deploy.sh')
    binaries = tmp_path / 'bin'
    binaries.mkdir()
    calls = tmp_path / 'calls.json'
    # The helper must neither invoke Compose nor need BOT_TOKEN/SSLIP_DOMAIN on the host.
    executable(binaries / 'docker', f'#!{sys.executable}\nimport json,os,sys\n'
               f'open({str(calls)!r}, "w").write(json.dumps(sys.argv[1:]))\n'
               'assert "SSLIP_DOMAIN" not in os.environ\n'
               'assert "BOT_TOKEN" not in os.environ\n'
               f'sys.exit({docker_status})\n')
    result = subprocess.run(['bash', str(tmp_path / 'deploy.sh'), 'renew-cert', *args],
                            env={'PATH': f'{binaries}:/usr/bin:/bin'}, capture_output=True, text=True)
    assert result.returncode == docker_status
    assert json.loads(calls.read_text()) == ['exec', 'nginx-ssl', '/renew-certificate.sh', *args]


@pytest.mark.parametrize('args', [['--force-renewal'], ['--dry-run', 'extra']])
def test_deploy_renew_rejects_unexpected_arguments(tmp_path, args):
    shutil.copyfile(ROOT / 'deploy.sh', tmp_path / 'deploy.sh')
    result = subprocess.run(['bash', str(tmp_path / 'deploy.sh'), 'renew-cert', *args],
                            env={'PATH': '/usr/bin:/bin'}, capture_output=True, text=True)
    assert result.returncode == 1
    assert 'Использование:' in result.stderr


@pytest.mark.parametrize(('enabled', 'certbot_status'), [('true', 0), ('true', 1), ('false', 0)])
def test_startup_renews_existing_custom_domain_and_preserves_on_failure(tmp_path, enabled, certbot_status):
    nginx_dir = tmp_path / 'nginx'
    (nginx_dir / 'templates').mkdir(parents=True)
    (nginx_dir / 'templates/nginx.conf.template').write_text('synthetic nginx config')
    le_dir = tmp_path / 'le'
    lineage = le_dir / 'live/downloads.example.org'
    lineage.mkdir(parents=True)
    (lineage / 'fullchain.pem').write_text('original-certificate')
    (lineage / 'privkey.pem').write_text('synthetic-key')
    entrypoint = tmp_path / 'entrypoint.sh'
    renew = tmp_path / 'renew-certificate.sh'
    for name, target in [('entrypoint.sh', entrypoint), ('renew-certificate.sh', renew)]:
        text = (ROOT / 'nginx' / name).read_text()
        text = text.replace('/etc/nginx', str(nginx_dir)).replace('/etc/letsencrypt', str(le_dir))
        text = text.replace('/var/www/certbot', str(tmp_path / 'webroot'))
        text = text.replace('/renew-certificate.sh', str(renew))
        executable(target, text)
    binaries = tmp_path / 'bin'
    binaries.mkdir()
    calls = tmp_path / 'calls'
    for name in ['nginx', 'certbot', 'envsubst', 'openssl', 'sleep']:
        if name == 'certbot':
            text = '#!/bin/sh\nprintf "certbot %s\\n" "$*" >> "$TEST_CALLS"\n'
            if certbot_status == 0:
                text += 'printf renewed-certificate > "$TEST_LINEAGE/fullchain.pem"\n'
            text += f'exit {certbot_status}\n'
        elif name == 'nginx':
            text = '#!/bin/sh\nprintf "nginx %s\\n" "$*" >> "$TEST_CALLS"\n'
        elif name == 'envsubst':
            text = '#!/bin/sh\ncat\n'
        else:
            text = '#!/bin/sh\nexit 0\n'
        executable(binaries / name, text)
    result = subprocess.run(['sh', str(entrypoint)], capture_output=True, text=True, timeout=10,
                            env={'PATH': f'{binaries}:/usr/bin:/bin', 'SSLIP_DOMAIN': 'downloads.example.org',
                                 'ENABLE_CERTBOT': enabled, 'TEST_CALLS': str(calls), 'TEST_LINEAGE': str(lineage)})
    assert result.returncode == 0, result.stderr
    history = calls.read_text()
    assert 'nginx -g daemon off;' in history
    assert 'Первичный запрос Certbot запущен' not in result.stdout
    if enabled == 'true':
        assert 'certbot renew --non-interactive --cert-name downloads.example.org' in history
        assert '--no-random-sleep-on-renew' in history
        assert 'certbot certonly' not in history and '--force-renewal' not in history
        if certbot_status:
            assert 'Продление не выполнено' in result.stdout
    else:
        assert 'certbot' not in history
    renewed = enabled == 'true' and certbot_status == 0
    assert ('nginx -s reload' in history) == renewed
    expected = 'renewed-certificate' if renewed else 'original-certificate'
    assert (nginx_dir / 'ssl/fullchain.pem').read_text() == expected
    assert (nginx_dir / 'ssl/privkey.pem').stat().st_mode & 0o777 == 0o600
