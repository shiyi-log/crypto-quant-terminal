"""使用真实临时文件核验研究产物，禁止用哈希冒充来源真实性。"""
import hashlib
import importlib
import importlib.util
import json
import os
import subprocess
import sys

import pytest

HOUR = 3_600_000
YEAR = 365 * 24 * HOUR


@pytest.fixture
def verifier():
    assert importlib.util.find_spec('research_artifact_verifier'), '缺少实际文件核验器'
    return importlib.import_module('research_artifact_verifier')


def spec(**changes):
    return dict(symbol='BTC/USDT:USDT', timeframe='1h', start_ms=0,
                end_ms=YEAR, as_of_ms=YEAR, source_ref='archive:btc-hourly-v1', **changes)


def candle(index=0, **changes):
    row = dict(timestamp_ms=index * HOUR, available_ms=(index + 1) * HOUR,
               closed=True, open='10.00000000000000000000000000000001',
               high='11', low='9', close='10', volume='0')
    row.update(changes)
    return row


def write_rows(path, rows):
    path.write_bytes(b''.join((json.dumps(row, separators=(',', ':')) + '\n').encode()
                             for row in rows))
    return path


def snapshot(verifier, path, **kwargs):
    return verifier.build_candle_snapshot(path, spec(), allowed_roots=[path.parent], **kwargs)


def test_one_year_real_zero_volume_stays_untrusted_and_reproducible(verifier, tmp_path):
    source = write_rows(tmp_path / 'candles.jsonl', (candle(i) for i in range(8760)))
    first = snapshot(verifier, source)
    second = snapshot(verifier, source)
    result = first.to_dict()
    assert first.canonical_json == second.canonical_json
    assert hashlib.sha256(first.canonical_json.encode()).hexdigest() == first.sha256
    assert result['source']['sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert result['quality']['expected_bars'] == result['quality']['observed_bars'] == 8760
    assert result['quality']['zero_volume_bars'] == 8760
    assert result['quality']['status'] == 'not_estimable'
    assert result['quality']['reason_codes'] == ['source_untrusted']
    assert result['acceptance_eligible'] is False
    assert result['source']['authenticated'] is False
    assert result['spec']['as_of_ms'] == YEAR


def test_snapshot_is_independent_of_machine_path(verifier, tmp_path):
    first = write_rows(tmp_path / 'a.jsonl', [candle()])
    second = write_rows(tmp_path / 'b.jsonl', [candle()])
    assert snapshot(verifier, first).canonical_json == snapshot(verifier, second).canonical_json


def test_decimal_precision_changes_quality_fingerprint(verifier, tmp_path):
    source = write_rows(tmp_path / 'a.jsonl', [candle()])
    first = snapshot(verifier, source).to_dict()
    write_rows(source, [candle(open='10.00000000000000000000000000000002')])
    second = snapshot(verifier, source).to_dict()
    assert first['source']['sha256'] != second['source']['sha256']
    assert first['quality']['data_sha256'] != second['quality']['data_sha256']


def test_duplicate_and_gap_are_not_counted_as_zero_volume(verifier, tmp_path):
    source = write_rows(tmp_path / 'a.jsonl', [candle(), candle()])
    quality = snapshot(verifier, source).to_dict()['quality']
    assert quality['observed_bars'] == 2
    assert quality['zero_volume_bars'] == 2
    assert {'candle_duplicate', 'candle_gap'} <= set(quality['reason_codes'])


@pytest.mark.parametrize('change', [dict(available_ms=YEAR + 1), dict(available_ms=0),
                                    dict(available_ms=True)])
def test_explicit_unavailable_time_fails_quality(verifier, tmp_path, change):
    source = write_rows(tmp_path / 'a.jsonl', [candle(**change)])
    assert 'candle_not_available' in snapshot(verifier, source).to_dict()['quality']['reason_codes']


def test_missing_availability_is_not_inferred_from_mtime(verifier, tmp_path):
    row = candle()
    del row['available_ms']
    source = write_rows(tmp_path / 'a.jsonl', [row])
    os.utime(source, (10, 10))
    with pytest.raises(verifier.ArtifactVerificationError, match='candle_schema_invalid'):
        snapshot(verifier, source)


@pytest.mark.parametrize('text', [
    '{"timestamp_ms":0,"timestamp_ms":1}',
    '{"open":1.5}', '{"open":NaN}', '{"open":Infinity}', '{"open":1e1}',
    '{"open":-Infinity}', '{"open":' + '9' * 150 + '}',
    '[]', '', '{"open":"未结束}',
])
def test_ambiguous_or_inexact_json_is_rejected(verifier, tmp_path, text):
    source = tmp_path / 'a.jsonl'
    source.write_text(text + '\n')
    with pytest.raises(verifier.ArtifactVerificationError):
        snapshot(verifier, source)


def test_file_cannot_claim_trusted_source(verifier, tmp_path):
    source = write_rows(tmp_path / 'a.jsonl', [candle(trusted_source=True)])
    with pytest.raises(verifier.ArtifactVerificationError, match='candle_schema_invalid'):
        snapshot(verifier, source)


@pytest.mark.parametrize('change', [dict(symbol='ETH/USDT:USDT'), dict(timeframe='5m')])
def test_candle_declaration_cannot_override_frozen_spec(verifier, tmp_path, change):
    source = write_rows(tmp_path / 'a.jsonl', [candle(**change)])
    with pytest.raises(verifier.ArtifactVerificationError, match='candle_spec_mismatch'):
        snapshot(verifier, source)


@pytest.mark.parametrize('mode', ['outside', 'traversal', 'symlink', 'directory_symlink', 'root_symlink'])
def test_unsafe_paths_never_enter_reader(verifier, tmp_path, mode):
    allowed = tmp_path / 'allowed'
    allowed.mkdir()
    outside = write_rows(tmp_path / 'outside.jsonl', [candle()])
    safe = write_rows(allowed / 'safe.jsonl', [candle()])
    roots = [allowed]
    if mode == 'outside':
        target = outside
    elif mode == 'traversal':
        target = allowed / '..' / 'outside.jsonl'
    elif mode == 'symlink':
        target = allowed / 'link.jsonl'
        target.symlink_to(safe)
    elif mode == 'directory_symlink':
        (allowed / 'link').symlink_to(allowed, target_is_directory=True)
        target = allowed / 'link' / 'safe.jsonl'
    else:
        link = tmp_path / 'root-link'
        link.symlink_to(allowed, target_is_directory=True)
        roots, target = [link], link / 'safe.jsonl'
    with pytest.raises(verifier.ArtifactVerificationError, match='path_unsafe'):
        verifier.build_candle_snapshot(target, spec(), allowed_roots=roots)


def test_fifo_is_rejected_without_blocking(verifier, tmp_path):
    source = tmp_path / 'pipe'
    os.mkfifo(source)
    with pytest.raises(verifier.ArtifactVerificationError, match='file_not_regular'):
        snapshot(verifier, source)


@pytest.mark.parametrize('mode', ['modify', 'replace'])
def test_actual_source_mutation_during_read_is_rejected(verifier, tmp_path, monkeypatch, mode):
    source = write_rows(tmp_path / 'a.jsonl', (candle(i) for i in range(1000)))
    real_read, triggered = os.read, False

    def changing_read(fd, size):
        nonlocal triggered
        data = real_read(fd, size)
        if data and not triggered:
            triggered = True
            if mode == 'modify':
                with source.open('r+b') as target:
                    target.seek(1)
                    target.write(b' ')
            else:
                replacement = write_rows(tmp_path / 'replacement', [candle()])
                replacement.replace(source)
        return data

    monkeypatch.setattr(verifier.os, 'read', changing_read)
    with pytest.raises(verifier.ArtifactVerificationError, match='source_changed'):
        snapshot(verifier, source)


@pytest.mark.parametrize('limit', [dict(max_bytes=10), dict(max_line_bytes=10), dict(max_rows=1)])
def test_reader_limits_are_enforced(verifier, tmp_path, limit):
    source = write_rows(tmp_path / 'a.jsonl', [candle(), candle(1)])
    with pytest.raises(verifier.ArtifactVerificationError, match='limit_exceeded'):
        snapshot(verifier, source, **limit)


def test_verified_artifact_binds_actual_bytes_expected_hash_media_and_reference(verifier, tmp_path):
    source = tmp_path / 'artifact.json'
    source.write_bytes(b'{"a":"1"}')
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    result = verifier.verify_local_artifact(source, allowed_roots=[tmp_path],
                                           expected_sha256=expected, media_type='application/json',
                                           reference='artifact:quality-v1').to_dict()
    assert result['sha256'] == expected
    assert result['uri'] == 'artifact:quality-v1'
    assert result['media_type'] == 'application/json'
    assert result['source_authenticated'] is False
    source.write_bytes(b'{"a":"2"}')
    with pytest.raises(verifier.ArtifactVerificationError, match='sha256_mismatch'):
        verifier.verify_local_artifact(source, allowed_roots=[tmp_path], expected_sha256=expected,
                                       media_type='application/json', reference='artifact:quality-v1')


@pytest.mark.parametrize('changes', [dict(media_type='text/html'), dict(reference='https://x/?key=secret'),
                                     dict(expected_sha256='g' * 64)])
def test_invalid_artifact_declarations_are_rejected(verifier, tmp_path, changes):
    source = tmp_path / 'artifact.json'
    source.write_bytes(b'{}')
    args = dict(allowed_roots=[tmp_path], expected_sha256=hashlib.sha256(b'{}').hexdigest(),
                media_type='application/json', reference='artifact:quality-v1')
    args.update(changes)
    with pytest.raises(verifier.ArtifactVerificationError, match='artifact_declaration_invalid'):
        verifier.verify_local_artifact(source, **args)


def test_output_is_content_addressed_and_never_overwrites(verifier, tmp_path):
    source = write_rows(tmp_path / 'a.jsonl', [candle()])
    result = snapshot(verifier, source)
    output = verifier.write_snapshot(result, tmp_path, allowed_roots=[tmp_path])
    assert output.name == result.sha256 + '.json'
    assert output.read_bytes() == result.canonical_json.encode()
    with pytest.raises(verifier.ArtifactVerificationError, match='output_exists'):
        verifier.write_snapshot(result, tmp_path, allowed_roots=[tmp_path])
    assert output.read_bytes() == result.canonical_json.encode()


def test_output_directory_symlink_and_forged_hash_are_rejected(verifier, tmp_path):
    source = write_rows(tmp_path / 'a.jsonl', [candle()])
    result = snapshot(verifier, source)
    link = tmp_path / 'link'
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(verifier.ArtifactVerificationError, match='path_unsafe'):
        verifier.write_snapshot(result, link, allowed_roots=[tmp_path])
    forged = type(result)(result.canonical_json, '0' * 64)
    with pytest.raises(verifier.ArtifactVerificationError, match='snapshot_invalid'):
        verifier.write_snapshot(forged, tmp_path, allowed_roots=[tmp_path])


def test_cli_emits_only_safe_codes_and_deterministic_output(verifier, tmp_path):
    source = write_rows(tmp_path / 'a.jsonl', [candle()])
    frozen = tmp_path / 'spec.json'
    frozen.write_text(json.dumps(spec()))
    command = [sys.executable, '-m', 'research_artifact_verifier', 'snapshot',
               '--source', str(source), '--spec', str(frozen), '--output-dir', str(tmp_path),
               '--allow-root', str(tmp_path)]
    first = subprocess.run(command, capture_output=True, text=True, check=False)
    assert first.returncode == 0, first.stderr
    assert 'source_untrusted' in first.stdout
    output = tmp_path / (snapshot(verifier, source).sha256 + '.json')
    assert output.exists()
    frozen.write_text('{"token":"VERY_PRIVATE", BROKEN}')
    failure = subprocess.run(command, capture_output=True, text=True, check=False)
    assert failure.returncode == 2
    assert 'VERY_PRIVATE' not in failure.stderr + failure.stdout
    assert '失败' in failure.stderr


@pytest.mark.parametrize('reference', ['artifact:quality@v1', 'unknown:quality-v1',
                                       'https://example.test:443/artifact', 'artifact:token:secret'])
def test_artifact_reference_is_safe_registry_compatible_subset(verifier, tmp_path, reference):
    source = tmp_path / 'artifact.json'
    source.write_bytes(b'{}')
    with pytest.raises(verifier.ArtifactVerificationError, match='artifact_declaration_invalid'):
        verifier.verify_local_artifact(source, allowed_roots=[tmp_path],
                                       expected_sha256=hashlib.sha256(b'{}').hexdigest(),
                                       media_type='application/json', reference=reference)


@pytest.mark.parametrize('changes', [dict(symbol=[]), dict(timeframe={}), dict(trusted_source=True),
                                     dict(as_of_ms=True)])
def test_invalid_frozen_declarations_fail_with_bounded_error(verifier, tmp_path, changes):
    source = write_rows(tmp_path / 'a.jsonl', [candle()])
    frozen = spec()
    frozen.update(changes)
    with pytest.raises(verifier.ArtifactVerificationError, match='spec_invalid'):
        verifier.build_candle_snapshot(source, frozen, allowed_roots=[tmp_path])


def test_written_bytes_can_be_reverified_for_registry_metadata(verifier, tmp_path):
    source = write_rows(tmp_path / 'a.jsonl', [candle()])
    result = snapshot(verifier, source)
    output = verifier.write_snapshot(result, tmp_path, allowed_roots=[tmp_path])
    metadata = verifier.verify_local_artifact(
        output, allowed_roots=[tmp_path], expected_sha256=result.sha256,
        media_type='application/json', reference='artifact:quality-v1').to_dict()
    assert metadata['sha256'] == output.stem
    assert metadata['size_bytes'] == len(result.canonical_json.encode())


def test_unknown_cli_arguments_do_not_echo_secrets(verifier):
    result = subprocess.run([sys.executable, '-m', 'research_artifact_verifier',
                             'snapshot', '--SECRET=VERY_PRIVATE'],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 2
    assert 'cli_arguments_invalid' in result.stderr
    assert 'SECRET' not in result.stderr
    assert 'VERY_PRIVATE' not in result.stderr


@pytest.mark.parametrize('raw', [b'\xff\n', b'\xef\xbb\xbf{}\n',
                                 b'{"open":"\\ud800"}\n', b'[' * 1500 + b']' * 1500])
def test_noncanonical_encoding_and_deep_json_fail_safely(verifier, tmp_path, raw):
    source = tmp_path / 'a.jsonl'
    source.write_bytes(raw)
    with pytest.raises(verifier.ArtifactVerificationError, match='json_invalid'):
        snapshot(verifier, source)


@pytest.mark.parametrize('change,reason', [
    (dict(volume='-0.1'), 'candle_volume_invalid'),
    (dict(open='NaN'), 'candle_ohlc_invalid'),
    (dict(open='1e999'), 'candle_ohlc_invalid'),
    (dict(low='12'), 'candle_ohlc_invalid'),
    (dict(closed=False), 'candle_not_closed'),
    (dict(timestamp_ms=1), 'coverage_grid_invalid'),
])
def test_invalid_exact_candle_facts_are_reported_without_repair(verifier, tmp_path, change, reason):
    source = write_rows(tmp_path / 'a.jsonl', [candle(**change)])
    result = snapshot(verifier, source).to_dict()['quality']
    assert reason in result['reason_codes']
    assert result['status'] == 'not_estimable'


def test_invalid_output_write_cleans_only_its_partial_file(verifier, tmp_path, monkeypatch):
    source = write_rows(tmp_path / 'a.jsonl', [candle()])
    result = snapshot(verifier, source)
    existing = tmp_path / 'keep.json'
    existing.write_bytes(b'preserve')

    def broken_write(fd, data):
        raise OSError('模拟磁盘写失败，不应回显原文')

    monkeypatch.setattr(verifier.os, 'write', broken_write)
    with pytest.raises(verifier.ArtifactVerificationError, match='output_unwritable'):
        verifier.write_snapshot(result, tmp_path, allowed_roots=[tmp_path])
    assert not (tmp_path / (result.sha256 + '.json')).exists()
    assert existing.read_bytes() == b'preserve'


def test_existing_output_symlink_is_never_followed_or_removed(verifier, tmp_path):
    source = write_rows(tmp_path / 'a.jsonl', [candle()])
    result = snapshot(verifier, source)
    protected = tmp_path / 'protected'
    protected.write_bytes(b'keep')
    output = tmp_path / (result.sha256 + '.json')
    output.symlink_to(protected)
    with pytest.raises(verifier.ArtifactVerificationError, match='output_exists'):
        verifier.write_snapshot(result, tmp_path, allowed_roots=[tmp_path])
    assert output.is_symlink()
    assert protected.read_bytes() == b'keep'


def test_final_line_without_newline_is_real_input_but_blank_lines_are_not(verifier, tmp_path):
    source = tmp_path / 'a.jsonl'
    source.write_text(json.dumps(candle()))
    assert snapshot(verifier, source).to_dict()['quality']['observed_bars'] == 1
    source.write_text(json.dumps(candle()) + '\n\n')
    with pytest.raises(verifier.ArtifactVerificationError, match='json_invalid'):
        snapshot(verifier, source)


def test_empty_file_reports_gap_and_never_manufactures_bars(verifier, tmp_path):
    source = tmp_path / 'a.jsonl'
    source.write_bytes(b'')
    quality = snapshot(verifier, source).to_dict()['quality']
    assert quality['observed_bars'] == quality['zero_volume_bars'] == 0
    assert quality['expected_bars'] == 8760
    assert quality['reason_codes'] == ['candle_gap', 'source_untrusted']


def test_verify_cli_is_real_byte_verification(verifier, tmp_path):
    source = tmp_path / 'artifact.json'
    source.write_bytes(b'{}')
    result = subprocess.run([
        sys.executable, '-m', 'research_artifact_verifier', 'verify', '--source', str(source),
        '--allow-root', str(tmp_path), '--expected-sha256', hashlib.sha256(b'{}').hexdigest(),
        '--media-type', 'application/json', '--reference', 'artifact:quality-v1',
    ], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value['sha256'] == hashlib.sha256(b'{}').hexdigest()
    assert value['source_authenticated'] is False


def test_nul_source_path_never_leaks_raw_open_exception(verifier, tmp_path):
    with pytest.raises(verifier.ArtifactVerificationError, match='path_unsafe'):
        verifier.build_candle_snapshot(str(tmp_path / 'bad') + '\x00private', spec(),
                                       allowed_roots=[tmp_path])


def test_replaced_output_is_detected_and_concurrent_file_is_preserved(verifier, tmp_path, monkeypatch):
    source = write_rows(tmp_path / 'a.jsonl', [candle()])
    result = snapshot(verifier, source)
    output = tmp_path / (result.sha256 + '.json')
    replacement = tmp_path / 'concurrent'
    replacement.write_bytes(b'concurrent content')
    real_write = os.write
    replaced = False

    def replacing_write(fd, data):
        nonlocal replaced
        written = real_write(fd, data)
        if not replaced:
            replaced = True
            output.unlink()
            output.symlink_to(replacement)
        return written

    monkeypatch.setattr(verifier.os, 'write', replacing_write)
    with pytest.raises(verifier.ArtifactVerificationError, match='output_changed'):
        verifier.write_snapshot(result, tmp_path, allowed_roots=[tmp_path])
    assert output.is_symlink()
    assert replacement.read_bytes() == b'concurrent content'


def test_cli_snapshot_summary_includes_actual_gap_reasons(verifier, tmp_path):
    source = write_rows(tmp_path / 'a.jsonl', [candle()])
    frozen = tmp_path / 'spec.json'
    frozen.write_text(json.dumps(spec()))
    result = subprocess.run([
        sys.executable, '-m', 'research_artifact_verifier', 'snapshot', '--source', str(source),
        '--spec', str(frozen), '--output-dir', str(tmp_path), '--allow-root', str(tmp_path),
    ], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['reason_codes'] == ['candle_gap', 'source_untrusted']


def test_writer_rejects_noncanonical_json_even_when_claimed_hash_matches(verifier, tmp_path):
    from research_protocol import FrozenResearchEvidence

    raw = '{ "schema_version": "research-candle-snapshot-v1" }'
    forged = FrozenResearchEvidence(raw, hashlib.sha256(raw.encode()).hexdigest())
    with pytest.raises(verifier.ArtifactVerificationError, match='snapshot_invalid'):
        verifier.write_snapshot(forged, tmp_path, allowed_roots=[tmp_path])
    assert not list(tmp_path.iterdir())
