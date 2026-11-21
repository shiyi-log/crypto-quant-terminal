"""共享快照的业务备份与隔离恢复校验；不管理服务，不在源库执行 DDL。

业务值在 PostgreSQL 内转换为带空值边界的文本数组后 SHA-256，客户端
只流式接收摘要。numeric/JSON 不经过 Python float，原始 json 格式保留。
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sys
from typing import Any

import psycopg
from psycopg import sql
from psycopg.pq import TransactionStatus

from tools import database_backup as backup

LOGGER = logging.getLogger(__name__)
MANIFEST_VERSION = 'quant-business-manifest-v1'
RESTORE_NAME = re.compile(r'quant_restore_[a-zA-Z0-9_]{1,48}')
DIGEST = re.compile(r'[0-9a-f]{64}')
IDENTITY_COLUMN = re.compile(r'(?:^|_)(?:strategy|account|stage|run|rule|execution|binding|capital|hash|sha256|fingerprint)(?:_|$)')


class VerificationError(backup.BackupError):
    """只公开有限步骤错误；不传播 DSN、SQL、业务值或服务端原始异常。"""


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _rows(conn, statement, parameters=()):
    cursor = conn.execute(statement, parameters)
    result = cursor.fetchall()
    if not result or isinstance(result[0], dict):
        return result
    names = [column.name for column in cursor.description]
    return [dict(zip(names, row)) for row in result]


def _scalar(conn, statement, parameters=()):
    row = conn.execute(statement, parameters).fetchone()
    return next(iter(row.values())) if isinstance(row, dict) else row[0]


def _schema(schema):
    if not isinstance(schema, str) or not schema or '\x00' in schema or len(schema.encode()) > 63:
        raise VerificationError('invalid schema identifier')


@contextmanager
def _snapshot(conn):
    """不提交调用者事务；已开启事务仅接受只读 REPEATABLE READ/SERIALIZABLE。"""
    idle = conn.info.transaction_status == TransactionStatus.IDLE
    if not idle:
        if (_scalar(conn, 'SHOW transaction_read_only') != 'on'
                or _scalar(conn, 'SHOW transaction_isolation') not in {'repeatable read', 'serializable'}):
            raise VerificationError('readonly repeatable snapshot transaction required')
    try:
        with conn.transaction():
            if idle:
                conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            conn.execute("SET LOCAL TimeZone TO 'UTC'")
            conn.execute("SET LOCAL DateStyle TO 'ISO, YMD'")
            conn.execute("SET LOCAL IntervalStyle TO 'postgres'")
            conn.execute('SET LOCAL extra_float_digits TO 3')
            conn.execute("SET LOCAL bytea_output TO 'hex'")
            conn.execute('SET LOCAL row_security TO off')
            conn.execute('SET LOCAL search_path TO pg_catalog')
            yield
    except VerificationError:
        raise
    except Exception:
        raise VerificationError('readonly database snapshot failed') from None


def _sequence_state(conn, schema):
    names = _rows(conn, '''SELECT c.relname AS name FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=%s AND c.relkind='S' ORDER BY c.relname''', (schema,))
    result = {}
    for item in names:
        name = item['name']
        values = _rows(conn, sql.SQL('SELECT last_value::text AS last_value,is_called FROM {}.{}')
                       .format(sql.Identifier(schema), sql.Identifier(name)))[0]
        definition = _rows(conn, '''SELECT s.seqstart::text AS start,s.seqincrement::text AS increment,
            s.seqmax::text AS maximum,s.seqmin::text AS minimum,s.seqcache::text AS cache,s.seqcycle AS cycle
            FROM pg_sequence s JOIN pg_class c ON c.oid=s.seqrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relname=%s''', (schema, name))[0]
        result[name] = {**values, **definition}
    return result


def _row_hashes(conn, schema, name, columns):
    """服务端排序摘要多集；重复行也逐项进入总摘要，客户端内存不随行数增长。"""
    expressions = sql.SQL(',').join(sql.SQL('{}::text').format(sql.Identifier(column)) for column in columns)
    statement = sql.SQL('''SELECT encode(sha256(convert_to(jsonb_build_array({})::text,'UTF8')),'hex') AS digest
        FROM {}.{} ORDER BY digest''').format(expressions, sql.Identifier(schema), sql.Identifier(name))
    digest, count = hashlib.sha256(), 0
    with conn.cursor(name='restore_hash_' + os.urandom(8).hex()) as cursor:
        cursor.execute(statement)
        for row in cursor:
            value = row['digest'] if isinstance(row, dict) else row[0]
            digest.update(bytes.fromhex(value))
            count += 1
    return {'row_count': count, 'rows_sha256': digest.hexdigest()}


def _catalog(conn, schema):
    from database_migrations import catalog_shape
    result = catalog_shape(conn, schema=schema)
    # 原始函数体逐字参与哈希；绝不改写字面量中的 schema 名。
    result['routines'] = _rows(conn, '''SELECT p.proname AS name,
        pg_get_function_identity_arguments(p.oid) AS arguments,p.prosrc AS body,
        p.probin AS binary,p.proconfig AS configuration,p.prosecdef AS security_definer,
        p.provolatile AS volatility,l.lanname AS language,pg_get_function_result(p.oid) AS result
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        JOIN pg_language l ON l.oid=p.prolang
        WHERE n.nspname=%s AND p.prokind IN ('f','p','w') ORDER BY p.proname,arguments''', (schema,))
    result['views'] = _rows(conn, '''SELECT c.relname AS name,c.relkind AS kind,
        pg_get_viewdef(c.oid,false) AS definition FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=%s AND c.relkind IN ('v','m') ORDER BY c.relname''', (schema,))
    result['policies'] = _rows(conn, '''SELECT c.relname AS table_name,p.polname AS name,
        p.polcmd AS command,p.polpermissive AS permissive,p.polroles::text AS roles,
        pg_get_expr(p.polqual,p.polrelid) AS qualification,
        pg_get_expr(p.polwithcheck,p.polrelid) AS with_check
        FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=%s ORDER BY c.relname,p.polname''', (schema,))
    result['partitions'] = _rows(conn, """SELECT c.relname AS name,c.relkind AS kind,
        c.relispartition AS is_partition,pg_get_partkeydef(c.oid) AS partition_key,
        pg_get_expr(c.relpartbound,c.oid,false) AS partition_bound,
        ARRAY(SELECT pn.nspname||'.'||p.relname FROM pg_inherits i
            JOIN pg_class p ON p.oid=i.inhparent JOIN pg_namespace pn ON pn.oid=p.relnamespace
            WHERE i.inhrelid=c.oid ORDER BY i.inhseqno) AS parents
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=%s AND c.relkind IN ('r','p') ORDER BY c.relname""", (schema,))
    result['column_collations'] = _rows(conn, """SELECT c.relname AS table_name,a.attname AS name,
        cn.nspname AS collation_schema,co.collname AS collation_name
        FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        LEFT JOIN pg_collation co ON co.oid=a.attcollation
        LEFT JOIN pg_namespace cn ON cn.oid=co.collnamespace
        WHERE n.nspname=%s AND a.attnum>0 AND NOT a.attisdropped
        AND c.relkind IN ('r','p','m') ORDER BY c.relname,a.attnum""", (schema,))
    result['types'] = _rows(conn, """SELECT t.typname AS name,t.typtype AS kind,
        pg_catalog.format_type(t.typbasetype,t.typtypmod) AS base_type,t.typnotnull AS required,
        t.typdefault AS default_expression,
        ARRAY(SELECT e.enumlabel FROM pg_enum e WHERE e.enumtypid=t.oid ORDER BY e.enumsortorder) AS enum_labels,
        ARRAY(SELECT pg_get_constraintdef(ct.oid,false) FROM pg_constraint ct
            WHERE ct.contypid=t.oid ORDER BY pg_get_constraintdef(ct.oid,false)) AS domain_constraints
        FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
        WHERE n.nspname=%s AND t.typtype IN ('d','e') ORDER BY t.typname""", (schema,))
    result['collations'] = _rows(conn, """SELECT co.collname AS name,co.collprovider AS provider,
        co.collisdeterministic AS deterministic,co.collencoding AS encoding,
        co.collcollate AS collate,co.collctype AS ctype,co.collversion AS version
        FROM pg_collation co JOIN pg_namespace n ON n.oid=co.collnamespace
        WHERE n.nspname=%s ORDER BY co.collname""", (schema,))
    return result


def capture_business_manifest(conn, schema='quant'):
    """读取全部表的精确多集、列、序列和冻结身份；不输出业务行或秘密。"""
    _schema(schema)
    with _snapshot(conn):
        if not _scalar(conn, 'SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=%s)', (schema,)):
            raise VerificationError('business schema missing')
        relations = _rows(conn, '''SELECT c.relname AS name,c.relkind AS kind
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=%s AND c.relkind IN ('r','p','m','f') ORDER BY c.relname''', (schema,))
        if any(row['kind'] == 'f' for row in relations):
            raise VerificationError('foreign table cannot be verified as archived business data')
        tables = {}
        for relation in relations:
            name = relation['name']
            columns = _rows(conn, '''SELECT a.attname AS name,
                format_type(a.atttypid,a.atttypmod) AS type,a.attnotnull AS required,
                a.attidentity AS identity,a.attgenerated AS generated,
                pg_get_expr(d.adbin,d.adrelid) AS default_expression
                FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
                JOIN pg_namespace n ON n.oid=c.relnamespace
                LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
                WHERE n.nspname=%s AND c.relname=%s AND a.attnum>0 AND NOT a.attisdropped
                ORDER BY a.attnum''', (schema, name))
            names = [row['name'] for row in columns]
            safe_columns = [{key: value for key, value in row.items() if key != 'default_expression'}
                            | {'default_sha256': _digest(row['default_expression'])} for row in columns]
            identities = [column for column in names if IDENTITY_COLUMN.search(column)]
            identity = _row_hashes(conn, schema, name, identities) if identities else None
            watermarks = {}
            for column in [column for column in names if column in {'sequence_id', 'account_sequence'}]:
                watermarks[column] = _rows(conn, sql.SQL('''SELECT min({})::text AS minimum,
                    max({})::text AS maximum,count(DISTINCT {}) AS distinct_count FROM {}.{}''')
                    .format(sql.Identifier(column), sql.Identifier(column), sql.Identifier(column),
                            sql.Identifier(schema), sql.Identifier(name)))[0]
            tables[name] = {**_row_hashes(conn, schema, name, names), 'kind': relation['kind'],
                            'columns': safe_columns, 'identity_sha256': _digest(identity),
                            'append_watermarks': watermarks}
            LOGGER.info('业务恢复指纹：表=%s，行数=%s，行摘要=%s', name, tables[name]['row_count'], tables[name]['rows_sha256'])
        return {'schema': MANIFEST_VERSION, 'namespace': schema, 'tables': tables,
                'sequences': _sequence_state(conn, schema), 'catalog_sha256': _digest(_catalog(conn, schema))}


def _validate_manifest(value):
    if (not isinstance(value, dict) or value.get('schema') != MANIFEST_VERSION
            or not isinstance(value.get('namespace'), str) or not value['namespace']
            or not isinstance(value.get('tables'), dict)
            or not isinstance(value.get('sequences'), dict)
            or not isinstance(value.get('catalog_sha256'), str)
            or not DIGEST.fullmatch(value['catalog_sha256'])):
        raise VerificationError('business manifest invalid')
    for name, record in value['tables'].items():
        if (not isinstance(name, str) or not isinstance(record, dict)
                or type(record.get('row_count')) is not int or record['row_count'] < 0
                or not isinstance(record.get('rows_sha256'), str)
                or not DIGEST.fullmatch(record['rows_sha256'])
                or not isinstance(record.get('columns'), list)):
            raise VerificationError('business manifest table invalid')


def compare_business_manifests(expected, actual):
    """精确比较键集合及摘要；差异仅报告对象名和类别，不返回行值。"""
    _validate_manifest(expected)
    _validate_manifest(actual)
    differences = []
    if expected['namespace'] != actual['namespace']:
        differences.append('namespace')
    for section in ('tables', 'sequences'):
        wanted, restored = expected[section], actual[section]
        for name in sorted(set(wanted) | set(restored)):
            if name not in restored:
                differences.append(section + ':missing:' + name)
            elif name not in wanted:
                differences.append(section + ':extra:' + name)
            elif wanted[name] != restored[name]:
                differences.append(section + ':changed:' + name)
    if expected['catalog_sha256'] != actual['catalog_sha256']:
        differences.append('catalog')
    if {key: value for key, value in expected.items() if key not in {'tables', 'sequences', 'namespace', 'catalog_sha256'}} != {
            key: value for key, value in actual.items() if key not in {'tables', 'sequences', 'namespace', 'catalog_sha256'}}:
        differences.append('manifest_fields')
    return {'status': 'mismatch' if differences else 'matched', 'differences': differences}


def _write_json(path, value):
    with Path(path).open('x', encoding='utf-8') as output:
        os.chmod(path, 0o600)
        output.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n')
        output.flush()
        os.fsync(output.fileno())


def backup_with_business_manifest(dsn, destination, *, schema='quant', container=None):
    """同一导出快照产生 pg_dump 和业务清单；序列观测漂移即保留失败产物。"""
    _schema(schema)
    config, environment = backup._connection(dsn, container)
    target = Path(destination).absolute()
    try:
        target.mkdir(mode=0o700, parents=True, exist_ok=False)
        archive = target / 'database.dump'
        with psycopg.connect(dsn, autocommit=True) as conn, _snapshot(conn):
            snapshot_id = _scalar(conn, 'SELECT pg_export_snapshot()')
            if not re.fullmatch(r'[0-9A-Fa-f-]+', snapshot_id):
                raise VerificationError('exported snapshot invalid')
            business = capture_business_manifest(conn, schema)
            LOGGER.info('数据库备份：已固定只读业务快照，开始 pg_dump')
            with archive.open('xb') as output:
                os.chmod(archive, 0o600)
                backup._run('pg_dump', ['--format=custom', '--no-password', '--snapshot=' + snapshot_id],
                            environment, container, output=output)
                output.flush()
                os.fsync(output.fileno())
            if _sequence_state(conn, schema) != business['sequences']:
                raise VerificationError('non-MVCC sequence state changed during backup')
        # pg_restore 继承底层文件描述符，不能把 Python 缓冲预读后的物理位置传给它。
        with archive.open('rb', buffering=0) as source:
            if source.read(5) != b'PGDMP':
                raise VerificationError('database archive format invalid')
            source.seek(0)
            backup._run('pg_restore', ['--list'], environment, container, source=source)
        business_path = target / 'business-manifest.json'
        _write_json(business_path, business)
        record = {'schema': 'quant-database-backup-v1', 'status': 'verified', 'created_at': backup._now(),
                  'source': {key: config[key] for key in ('host', 'port', 'dbname') if key in config},
                  'transport': 'docker' if container else 'client',
                  'archive': {'file': archive.name, 'size_bytes': archive.stat().st_size, 'sha256': backup._hash(archive)},
                  'business_manifest': {'file': business_path.name, 'sha256': backup._hash(business_path),
                                        'snapshot_consistent': True, 'snapshot_scope': 'MVCC table rows and catalog',
                                        'sequence_validation': 'stable_observation_restore_comparison_required'}}
        stage = target / 'manifest.json.partial'
        _write_json(stage, record)
        stage.replace(target / 'manifest.json')
        LOGGER.info('数据库备份：归档与业务清单哈希已发布，仍需隔离恢复校验')
        return target / 'manifest.json'
    except backup.BackupError:
        raise
    except Exception:
        raise VerificationError('business backup failed; success manifest not published') from None


# 由独立 quant_test 空 schema 执行冻结 Demo DDL 后生成；新迁移没有物理锚点即拒绝。
DEMO_PHYSICAL_ANCHORS = {'0001_demo_risk_events': {'demo_account': '12add6e16b9dacc8710f0b8a44542ff955fca33586c68213163e517c27bc4618', 'demo_fills': 'f42713012e3475b5dae396f2eeb12b135350ac7cc9849d26fce85c456ad4ab2a', 'demo_orders': 'de6ce0e78d4ea34fcb7d0eaec46970c6f4ea6f9d7dfffd887f470ba2dab9cbbf', 'demo_positions': '1a4b4763157cfc2fadcea4c9be490153acec600626687cb1ec494763ebf60168', 'demo_risk_events': '4536dfa9bf7671378c1ad517297e76c6e990058bbe62051077383f88861eb316', 'demo_schema_migrations': '476f2d6adb5cf37d4b5842cc93db1deef38b6e7ef63c3b1b60f096b84a785f89', 'demo_signal_comparisons': '38152d10b1ecf8703e02272024876b8f081a324728dc27e83ed8f37fd4a9d3ae'}, '0002_risk_event_integrity': {'demo_account': '12add6e16b9dacc8710f0b8a44542ff955fca33586c68213163e517c27bc4618', 'demo_fills': 'f42713012e3475b5dae396f2eeb12b135350ac7cc9849d26fce85c456ad4ab2a', 'demo_orders': 'de6ce0e78d4ea34fcb7d0eaec46970c6f4ea6f9d7dfffd887f470ba2dab9cbbf', 'demo_positions': '1a4b4763157cfc2fadcea4c9be490153acec600626687cb1ec494763ebf60168', 'demo_risk_events': '7524fa612dccd02f062ffdd7824c8c9336f8ee7715f7edc5bca34abf35abf416', 'demo_schema_migrations': '476f2d6adb5cf37d4b5842cc93db1deef38b6e7ef63c3b1b60f096b84a785f89', 'demo_signal_comparisons': '38152d10b1ecf8703e02272024876b8f081a324728dc27e83ed8f37fd4a9d3ae'}, '0003_native_conditional_orders': {'demo_account': '12add6e16b9dacc8710f0b8a44542ff955fca33586c68213163e517c27bc4618', 'demo_conditional_current': '9c0daafe1fa714f9b64876adc24e31beb6ef2a7fb6085ff4f865582a6c035455', 'demo_conditional_events': '8c54031aade63746372a381039b39c585862ee78ab5bb492c1f5853067229114', 'demo_conditional_intents': '82b2373a40007271265a4a4506dd05baa5c15ce7f1b68151fcc0db52e25e64b3', 'demo_fills': 'f42713012e3475b5dae396f2eeb12b135350ac7cc9849d26fce85c456ad4ab2a', 'demo_orders': 'de6ce0e78d4ea34fcb7d0eaec46970c6f4ea6f9d7dfffd887f470ba2dab9cbbf', 'demo_positions': '1a4b4763157cfc2fadcea4c9be490153acec600626687cb1ec494763ebf60168', 'demo_risk_events': '7524fa612dccd02f062ffdd7824c8c9336f8ee7715f7edc5bca34abf35abf416', 'demo_schema_migrations': '476f2d6adb5cf37d4b5842cc93db1deef38b6e7ef63c3b1b60f096b84a785f89', 'demo_signal_comparisons': '38152d10b1ecf8703e02272024876b8f081a324728dc27e83ed8f37fd4a9d3ae'}, '0004_entry_risk_reservations': {'demo_account': '12add6e16b9dacc8710f0b8a44542ff955fca33586c68213163e517c27bc4618', 'demo_conditional_current': '9c0daafe1fa714f9b64876adc24e31beb6ef2a7fb6085ff4f865582a6c035455', 'demo_conditional_events': '8c54031aade63746372a381039b39c585862ee78ab5bb492c1f5853067229114', 'demo_conditional_intents': '82b2373a40007271265a4a4506dd05baa5c15ce7f1b68151fcc0db52e25e64b3', 'demo_entry_decisions': '8a620bee76664878006bc0805e3ade19dfa9f8df3df557673a04d415cb260056', 'demo_fills': 'f42713012e3475b5dae396f2eeb12b135350ac7cc9849d26fce85c456ad4ab2a', 'demo_orders': 'd0f540f503c6c93cf092810a341823baffe69b2c5db636c035988d09e5f5413e', 'demo_positions': '1a4b4763157cfc2fadcea4c9be490153acec600626687cb1ec494763ebf60168', 'demo_risk_contexts': '88c67cb38591a58e0320f32184007e8d22dcf15aa210ff8ce0f4457b829f10e9', 'demo_risk_events': '7524fa612dccd02f062ffdd7824c8c9336f8ee7715f7edc5bca34abf35abf416', 'demo_risk_flat_reconciliations': '04cde872c173e7ae5a7a204d35ea5f44580790d023dd0a0aacc781d7541d8cc4', 'demo_risk_reservation_events': '6fd790c12b47c351235248272cbca4d3fd602646964cd13abbd60c0eff240582', 'demo_risk_reservations': '323b2f2ee5c5d578e60ae4b81421f513e71d42903f3c18f44dba794b61dc7f12', 'demo_schema_migrations': '476f2d6adb5cf37d4b5842cc93db1deef38b6e7ef63c3b1b60f096b84a785f89', 'demo_signal_comparisons': '38152d10b1ecf8703e02272024876b8f081a324728dc27e83ed8f37fd4a9d3ae'}, '0005_account_evidence_archive': {'demo_account': '12add6e16b9dacc8710f0b8a44542ff955fca33586c68213163e517c27bc4618', 'demo_completion_records': '3042a747cf08d01aea56eb82e13c8c8ac8de3ea5919cf0c70a72b478840cdf1b', 'demo_conditional_current': '9c0daafe1fa714f9b64876adc24e31beb6ef2a7fb6085ff4f865582a6c035455', 'demo_conditional_events': '8c54031aade63746372a381039b39c585862ee78ab5bb492c1f5853067229114', 'demo_conditional_intents': '82b2373a40007271265a4a4506dd05baa5c15ce7f1b68151fcc0db52e25e64b3', 'demo_entry_decisions': '8a620bee76664878006bc0805e3ade19dfa9f8df3df557673a04d415cb260056', 'demo_evidence_archive': '30bf2e9bb287a61b29d33549077d70e5549ca79c01a7ccb9e6f2b85f840d24e6', 'demo_fills': 'f42713012e3475b5dae396f2eeb12b135350ac7cc9849d26fce85c456ad4ab2a', 'demo_orders': 'd0f540f503c6c93cf092810a341823baffe69b2c5db636c035988d09e5f5413e', 'demo_positions': '1a4b4763157cfc2fadcea4c9be490153acec600626687cb1ec494763ebf60168', 'demo_risk_contexts': '88c67cb38591a58e0320f32184007e8d22dcf15aa210ff8ce0f4457b829f10e9', 'demo_risk_events': '7524fa612dccd02f062ffdd7824c8c9336f8ee7715f7edc5bca34abf35abf416', 'demo_risk_flat_reconciliations': '04cde872c173e7ae5a7a204d35ea5f44580790d023dd0a0aacc781d7541d8cc4', 'demo_risk_reservation_events': '6fd790c12b47c351235248272cbca4d3fd602646964cd13abbd60c0eff240582', 'demo_risk_reservations': '323b2f2ee5c5d578e60ae4b81421f513e71d42903f3c18f44dba794b61dc7f12', 'demo_schema_migrations': '476f2d6adb5cf37d4b5842cc93db1deef38b6e7ef63c3b1b60f096b84a785f89', 'demo_signal_comparisons': '38152d10b1ecf8703e02272024876b8f081a324728dc27e83ed8f37fd4a9d3ae'}, 'baseline': {'demo_account': '12add6e16b9dacc8710f0b8a44542ff955fca33586c68213163e517c27bc4618', 'demo_fills': 'f42713012e3475b5dae396f2eeb12b135350ac7cc9849d26fce85c456ad4ab2a', 'demo_orders': 'de6ce0e78d4ea34fcb7d0eaec46970c6f4ea6f9d7dfffd887f470ba2dab9cbbf', 'demo_positions': '1a4b4763157cfc2fadcea4c9be490153acec600626687cb1ec494763ebf60168', 'demo_schema_migrations': '476f2d6adb5cf37d4b5842cc93db1deef38b6e7ef63c3b1b60f096b84a785f89', 'demo_signal_comparisons': '38152d10b1ecf8703e02272024876b8f081a324728dc27e83ed8f37fd4a9d3ae'}}

# 0006 只新增独立的研究注册表，不改变 Demo 表与触发器。
# Demo 物理结构继续使用 0005 冻结锚点；研究表仍参与完整业务指纹比对。
DEMO_PHYSICAL_ANCHORS['0006_research_registry'] = DEMO_PHYSICAL_ANCHORS['0005_account_evidence_archive']

# 在独立 quant_test 随机 schema 安装完整 0007 后读取 catalog：
# 旧表摘要逐一相同；下列三表新增约束、索引和 ALWAYS 不可变触发器。
# 完整 Demo 结构摘要：50e16cab88edd1146da7af90cdc2ad803509d504d84d8779d2d944c2c74d2846。
DEMO_PHYSICAL_ANCHORS['0007_writer_commands'] = {
    **DEMO_PHYSICAL_ANCHORS['0006_research_registry'],
    'demo_command_bindings': '643ef3ff8a8468591657396f9b4e9da2dc8caed23b6abd2fc45507c7bd308c10',
    'demo_command_events': '62fa29584fbe0da2d9f0399a121653961be1d1de3a33b280635aa94de1122ee8',
    'demo_command_intents': 'bb451c4c4d966560882eff90fb7fe0aa171e9ee2c910d8f1d9082fac2851f745',
}

# 在独立 quant_test 随机 schema 安装 0008 后读取 catalog；仅新增四张
# 只追加研究事实表，表摘要包含约束、索引及 ENABLE ALWAYS 不可变触发器。
DEMO_PHYSICAL_ANCHORS['0008_research_two_stage'] = {
    **DEMO_PHYSICAL_ANCHORS['0007_writer_commands'],
    'refactor_research_candidate_results_v2': 'a99bee5afe2b3ca155b64ec3abe568dc43eaf5972e27cf316b3e67b0a78ce730',
    'refactor_research_inferences_v2': '1a479a0becfb10843059938a6442e0c648ffb897e5c80ea3f97538b592e5ab90',
    'refactor_research_selections_v2': '6df60d3190616af1de3725c3c3f980bd0c2b37184a6a9ee1db0ccd8982af7394',
    'refactor_research_validation_results_v2': '797d71210ee8d4c24d31cdebb886686fc9f3a85869416b9afbe19335fbd9a335',
}

# 由隔离 quant_test 随机 schema 只安装 0008/0009 后实测；领取仍未接入代码。
DEMO_PHYSICAL_ANCHORS['0009_research_holdout_claims'] = {
    **DEMO_PHYSICAL_ANCHORS['0008_research_two_stage'],
    'refactor_research_candidate_results_v2': '0bc678a059f3a051b5b9a3fc6d6a92f14246148a35ae64db9830835f6a82fdc7',
    'refactor_research_holdout_claims_v2': 'dbd8d5b407e8bad10646e154b37d80d0241b1e479f9435386f2a7f09d1d53094',
}

def _demo_physical_shape(conn, schema):
    from database_migrations import catalog_shape
    catalog = catalog_shape(conn, schema=schema)
    flags = _rows(conn, """SELECT c.relname AS table_name,a.attname AS name,
        a.attidentity AS identity,a.attgenerated AS generated,
        cn.nspname AS collation_schema,co.collname AS collation_name
        FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        LEFT JOIN pg_collation co ON co.oid=a.attcollation
        LEFT JOIN pg_namespace cn ON cn.oid=co.collnamespace
        WHERE n.nspname=%s AND a.attnum>0 AND NOT a.attisdropped
        AND c.relkind IN ('r','p') ORDER BY c.relname,a.attnum""", (schema,))
    catalog['column_flags'] = flags
    ownership = _rows(conn, """SELECT c.relname AS table_name,a.attname AS column_name,
        seq.relname AS sequence_name,s.seqtypid::regtype::text AS type,
        s.seqstart::text AS start,s.seqincrement::text AS increment,
        s.seqmax::text AS maximum,s.seqmin::text AS minimum,s.seqcache::text AS cache,s.seqcycle AS cycle,
        d.deptype AS dependency
        FROM pg_depend d JOIN pg_class seq ON seq.oid=d.objid AND seq.relkind='S'
        JOIN pg_sequence s ON s.seqrelid=seq.oid JOIN pg_class c ON c.oid=d.refobjid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=d.refobjsubid
        WHERE n.nspname=%s AND d.classid='pg_class'::regclass
        AND d.refclassid='pg_class'::regclass AND d.deptype IN ('a','i')
        ORDER BY c.relname,a.attname""", (schema,))
    catalog['sequence_ownership'] = ownership
    names = [row['table_name'] for row in catalog['relations']]
    return {name: _digest({kind: sorted((row for row in rows if row.get('table_name') == name), key=_json)
                          for kind, rows in catalog.items()}) for name in names}


def _validate_demo_physical(conn, schema, existing):
    prefix = next(reversed(existing), 'baseline')
    expected = DEMO_PHYSICAL_ANCHORS.get(prefix)
    if not expected:
        raise VerificationError('Demo physical migration anchor missing')
    actual = _demo_physical_shape(conn, schema)
    if any(actual.get(name) != digest for name, digest in expected.items()):
        raise VerificationError('Demo physical migration shape mismatch')
    # 不允许未知 Demo 表借用已知迁移历史；其它全局/独立域由各自契约负责。
    if any(name.startswith('demo_') and name not in expected for name in actual):
        raise VerificationError('Demo physical migration contains unknown table')
    return _digest(expected)


def validate_database_invariants(conn, *, schema='quant', allow_legacy_baseline=False):
    """只读检查约束/索引及代码冻结迁移；旧库接管必须显式选用 legacy 模式。"""
    _schema(schema)
    with _snapshot(conn):
        invalid = _scalar(conn, '''SELECT count(*) FROM pg_constraint ct
            JOIN pg_namespace n ON n.oid=ct.connamespace WHERE n.nspname=%s AND NOT ct.convalidated''', (schema,))
        if invalid:
            raise VerificationError('unvalidated database constraint')
        invalid = _scalar(conn, '''SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid=i.indrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=%s AND (NOT i.indisvalid OR NOT i.indisready)''', (schema,))
        if invalid:
            raise VerificationError('invalid database constraint index')
        import database_migrations
        try:
            present = _scalar(conn, '''SELECT EXISTS(SELECT 1 FROM pg_class c
                JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname=%s AND c.relname='global_schema_migrations' AND c.relkind='r')''', (schema,))
            if present:
                global_result = database_migrations.check_schema(conn, schema=schema)
                migration_mode = 'current'
            elif allow_legacy_baseline:
                global_result = {'shape_sha256': database_migrations._verify_shape(conn, schema)}
                migration_mode = 'legacy_baseline_compatible_adoption_pending'
            else:
                raise VerificationError('global migration history missing')
            demo_present = _scalar(conn, '''SELECT EXISTS(SELECT 1 FROM pg_class c
                JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname=%s AND c.relname='demo_schema_migrations' AND c.relkind='r')''', (schema,))
            demo_tables = _scalar(conn, '''SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname=%s AND c.relkind IN ('r','p') AND left(c.relname,5)='demo_' ''', (schema,))
            if demo_tables and not demo_present:
                raise VerificationError('Demo migration history missing')
            if demo_present:
                from demo_store import _demo_migration_bundle, _validate_demo_migration_records
                records = _rows(conn, sql.SQL('SELECT migration_id,checksum FROM {}.demo_schema_migrations ORDER BY migration_id')
                                .format(sql.Identifier(schema)))
                existing = {row['migration_id']: row['checksum'] for row in records}
                if len(existing) != len(records):
                    raise VerificationError('duplicate Demo migration history')
                bundle = _demo_migration_bundle()
                _validate_demo_migration_records(existing, bundle, require_current=not allow_legacy_baseline)
                demo_physical_sha256 = _validate_demo_physical(conn, schema, existing)
                demo_mode = ('current' if set(existing) == set(bundle) else
                             'known_prefix_pending_explicit_migration')
        except VerificationError:
            raise
        except Exception:
            raise VerificationError('database migration compatibility check failed') from None
        return {'constraints': 'passed', 'migrations': migration_mode,
                'global_shape_sha256': global_result['shape_sha256'],
                'demo_migrations': demo_mode if demo_present else 'not_present',
                'demo_physical_sha256': demo_physical_sha256 if demo_present else None}


def _read_json(path, message):
    try:
        path = Path(path)
        if path.is_symlink() or not path.is_file():
            raise ValueError
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        raise VerificationError(message) from None


def verify_restored_database(conn, backup_dir, restore_receipt, *, schema='quant', allow_legacy_baseline=False):
    """可信本地归档/清单/恢复回执全链匹配后才返回 passed；从不写数据库。"""
    database = conn.info.dbname
    if not isinstance(database, str) or not RESTORE_NAME.fullmatch(database):
        raise VerificationError('business verification requires isolated quant_restore_* database')
    _, record = backup._verified_archive(backup_dir)
    receipt = _read_json(restore_receipt, 'restore receipt invalid')
    if (not isinstance(receipt, dict) or receipt.get('status') != 'restored'
            or receipt.get('database') != database
            or receipt.get('archive_sha256') != record['archive']['sha256']):
        raise VerificationError('restore receipt does not match archive and target')
    business_record = record.get('business_manifest')
    if (not isinstance(business_record, dict) or business_record.get('file') != 'business-manifest.json'
            or business_record.get('snapshot_consistent') is not True):
        raise VerificationError('trusted snapshot business manifest missing')
    manifest_path = Path(backup_dir) / 'business-manifest.json'
    expected = _read_json(manifest_path, 'trusted business manifest invalid')
    if backup._hash(manifest_path) != business_record.get('sha256'):
        raise VerificationError('trusted business manifest hash mismatch')
    _validate_manifest(expected)
    if expected['namespace'] != schema:
        raise VerificationError('business manifest namespace mismatch')
    with _snapshot(conn):
        actual = capture_business_manifest(conn, schema)
        comparison = compare_business_manifests(expected, actual)
        if comparison['status'] != 'matched':
            raise VerificationError('restored business fingerprint mismatch: ' + ','.join(comparison['differences']))
        invariants = validate_database_invariants(conn, schema=schema, allow_legacy_baseline=allow_legacy_baseline)
    result = {'status': 'restored', 'database': database, 'archive_sha256': record['archive']['sha256'],
              'verified_namespace': schema, 'coverage': 'scoped_schema',
              'schema': 'quant-database-restore-verification-v1', 'reference': str(Path(restore_receipt).absolute()),
              'verified_at': backup._now(), 'business_validation': 'passed',
              'business_manifest_sha256': business_record['sha256'], 'catalog_sha256': actual['catalog_sha256'],
              'table_count': len(actual['tables']), 'row_count': sum(item['row_count'] for item in actual['tables'].values()),
              'constraint_validation': 'passed', 'migration_validation': invariants,
              'sequence_validation': 'exact_restored_state_matched'}
    LOGGER.info('隔离恢复验证通过：表数=%s，行数=%s，迁移模式=%s', result['table_count'], result['row_count'], invariants['migrations'])
    return result


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse 默认回显未知参数，可能把误传的 DSN 写入日志。
        self.print_usage(sys.stderr)
        self.exit(2, 'invalid command arguments; connection values must use environment variables\n')


def main(argv=None):
    parser = _SafeArgumentParser(description='只读业务指纹与隔离数据库恢复演练；连接只从环境读取')
    parser.add_argument('action', choices=['capture', 'backup', 'restore', 'verify'])
    parser.add_argument('--schema', default='quant')
    parser.add_argument('--backup-dir')
    parser.add_argument('--receipt')
    parser.add_argument('--output', required=True)
    parser.add_argument('--container')
    parser.add_argument('--allow-legacy-baseline', action='store_true')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        output = Path(args.output)
        if output.exists():
            raise VerificationError('output must be a new file')
        if args.action in {'backup', 'restore', 'verify'} and not args.backup_dir:
            raise VerificationError('backup directory required')
        variable = 'QUANT_RESTORE_DATABASE_URL' if args.action in {'restore', 'verify'} else 'QUANT_DATABASE_URL'
        dsn = os.getenv(variable)
        if not dsn:
            raise VerificationError('database environment variable required')
        if args.action == 'backup':
            result = backup_with_business_manifest(dsn, args.backup_dir, schema=args.schema, container=args.container)
            _write_json(output, {'status': 'backup_created', 'manifest': str(result)})
        elif args.action == 'restore':
            _write_json(output, backup.restore_database(args.backup_dir, dsn, container=args.container))
        else:
            if args.action == 'verify' and not args.receipt:
                raise VerificationError('trusted restore receipt required')
            if args.action == 'verify':
                config, _ = backup._connection(dsn, args.container)
                if (config.get('host') not in {'localhost', '127.0.0.1', '::1'}
                        or config.get('hostaddr', '127.0.0.1') not in {'127.0.0.1', '::1'}
                        or not RESTORE_NAME.fullmatch(config['dbname'])):
                    raise VerificationError('verification requires isolated local quant_restore_* target')
            with psycopg.connect(dsn, autocommit=True) as conn:
                result = (capture_business_manifest(conn, args.schema) if args.action == 'capture' else
                          verify_restored_database(conn, args.backup_dir, args.receipt, schema=args.schema,
                                                   allow_legacy_baseline=args.allow_legacy_baseline))
            _write_json(output, result)
        print(json.dumps({'status': 'completed', 'output': str(output.absolute())}, ensure_ascii=False))
        return 0
    except backup.BackupError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        print('database verification operation failed', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
