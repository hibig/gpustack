import importlib.util
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy.dialects import mysql, postgresql


def test_dashboard_schema_upgrade_and_downgrade_are_retryable(monkeypatch):
    path = (
        Path(__file__).resolve().parents[2]
        / 'gpustack/migrations/versions/2026_07_15_1600-367a3982fcde_v2_3_0_database_changes.py'
    )
    spec = importlib.util.spec_from_file_location('dashboard_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    tables = ('model_usages', 'model_usage_details', 'model_usage_details_archive')
    columns = {name: {'consumer_principal_id', 'date'} for name in tables}
    columns['model_usage_details'].add('cluster_id')
    columns['model_usage_details_archive'].add('cluster_id')
    indexes = {name: set() for name in tables}
    added = []

    def add_column(name, column):
        assert column.name not in columns[name]
        for dialect in (postgresql.dialect(), mysql.dialect()):
            assert str(column.type.compile(dialect=dialect))
        columns[name].add(column.name)
        added.append((name, column.name))

    monkeypatch.setattr(migration, 'table_exists', lambda name: name in columns)
    monkeypatch.setattr(
        migration, 'column_exists', lambda name, column: column in columns[name]
    )
    monkeypatch.setattr(
        migration, '_index_exists', lambda name, index: index in indexes[name]
    )
    monkeypatch.setattr(
        migration,
        'op',
        SimpleNamespace(
            add_column=add_column,
            drop_column=lambda name, column: columns[name].remove(column),
            create_index=lambda index, name, keys: indexes[name].add(index),
            drop_index=lambda index, table_name: indexes[table_name].remove(index),
        ),
    )
    migration._upgrade_dashboard_usage()
    first = list(added)
    migration._upgrade_dashboard_usage()
    assert added == first
    assert len(added) == 7
    assert all(len(indexes[name]) == 1 for name in tables)
    migration._downgrade_dashboard_usage()
    migration._downgrade_dashboard_usage()
    assert 'cluster_id' not in columns['model_usages']
    assert 'cluster_id' in columns['model_usage_details']
    assert not any(indexes.values())
