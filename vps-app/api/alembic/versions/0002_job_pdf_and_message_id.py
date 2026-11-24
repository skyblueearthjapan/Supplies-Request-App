"""jobs.pdf_file_id と mail_log.message_id_header

- jobs.pdf_file_id: その仕事で作った PDF。メールで失敗して送り直すとき、PDF を作り直さない。
- mail_log.message_id_header: 送る前に決めた Message-ID。時間切れの後は、送信済みかを確かめてから送り直す。

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-24 21:00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('pdf_file_id', sa.String(length=256), server_default='', nullable=False))
    with op.batch_alter_table('mail_log', schema=None) as batch_op:
        batch_op.add_column(sa.Column('message_id_header', sa.String(length=256), server_default='', nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('mail_log', schema=None) as batch_op:
        batch_op.drop_column('message_id_header')
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_column('pdf_file_id')
