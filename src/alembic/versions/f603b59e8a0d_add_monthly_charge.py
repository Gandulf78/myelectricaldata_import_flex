"""Add monthly_charge to ConsumptionDaily

Revision ID: f603b59e8a0d
Revises: 
Create Date: 2024-01-11 

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f603b59e8a0d'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('consumption_daily', sa.Column('monthly_charge', sa.Float(), nullable=True, server_default='0'))
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column('consumption_daily', 'monthly_charge')
    # ### end Alembic commands ###
