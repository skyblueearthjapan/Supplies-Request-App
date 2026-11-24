"""業務ロジック（Workflow.gs / Helpers.gs / DeptSupervisors.gs / Recipients.gs の移植）。担当 W。

- labels.py   画面に配る定数（Constants.gs の REASONS・*_LABELS など）
- helpers.py  Helpers.gs の小道具（文字の整え方・数の読み方・税・toClientRequest_）
- store.py    表の読み書き（Storage.gs の代わり。申請の行ロック・明細の書き換え・履歴・設定）
- masters.py  承認者マスタ・上席マスタの探し方と権限の判定（Workflow.gs 後半・DeptSupervisors.gs）
- outbox.py   後回しの仕事（Jobs.gs の defer_ の代わり。models.Job に積むだけ）
- workflow.py 申請の操作（画面から呼ぶ関数）
- admin.py    getBootstrap・管理画面の保存・同期・テスト転送・失敗した後処理
- register.py rpc への登録
"""
