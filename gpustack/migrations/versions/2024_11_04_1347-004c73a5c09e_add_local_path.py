"""add local_path

Revision ID: 004c73a5c09e
Revises: 0f7dde8b3c4b
Create Date: 2024-11-04 13:47:01.461783

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel
import gpustack


# revision identifiers, used by Alembic.
revision: str = '004c73a5c09e'
down_revision: Union[str, None] = '0f7dde8b3c4b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# https://alembic.sqlalchemy.org/en/latest/batch.html#dropping-unnamed-or-named-foreign-key-constraints
naming_convention = {
    "fk":
    "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
}

def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('model_instances', schema=None) as batch_op:
        batch_op.add_column(sa.Column('local_path', sqlmodel.sql.sqltypes.AutoString(), nullable=True))

    with op.batch_alter_table('models', schema=None) as batch_op:
        batch_op.add_column(sa.Column('local_path', sqlmodel.sql.sqltypes.AutoString(), nullable=True))


    with op.batch_alter_table('model_usages', naming_convention=naming_convention) as batch_op:
        batch_op.drop_constraint('fk_model_usages_user_id_users', type_='foreignkey')
        batch_op.drop_constraint('fk_model_usages_model_id_models', type_='foreignkey')
        batch_op.create_foreign_key('fk_model_usages_user_id_users', 'users', ['user_id'], ['id'], ondelete='CASCADE')
        batch_op.create_foreign_key('fk_model_usages_model_id_models', 'models', ['model_id'], ['id'], ondelete='CASCADE')
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('model_usages', naming_convention=naming_convention) as batch_op:
        batch_op.drop_constraint('fk_model_usages_user_id_users', type_='foreignkey')
        batch_op.drop_constraint('fk_model_usages_model_id_models', type_='foreignkey')
        batch_op.create_foreign_key('fk_model_usages_user_id_users', 'users', ['user_id'], ['id'])
        batch_op.create_foreign_key('fk_model_usages_model_id_models', 'models', ['model_id'], ['id'])

    with op.batch_alter_table('models', schema=None) as batch_op:
        batch_op.drop_column('local_path')

    with op.batch_alter_table('model_instances', schema=None) as batch_op:
        batch_op.drop_column('local_path')

    # ### end Alembic commands ###