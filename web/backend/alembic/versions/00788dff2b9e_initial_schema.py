"""initial_schema

Revision ID: 00788dff2b9e
Revises: 
Create Date: 2026-04-02 18:07:26

All 28 tables from NexusTrader desktop schema + 2 web-only auth tables.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "00788dff2b9e"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE agent_signals (
	id SERIAL NOT NULL, 
	agent_name VARCHAR(50) NOT NULL, 
	timestamp TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	signal FLOAT NOT NULL, 
	confidence FLOAT NOT NULL, 
	is_stale BOOLEAN NOT NULL, 
	symbol VARCHAR(20), 
	topic VARCHAR(100) NOT NULL, 
	payload JSONB, 
	regime_bias VARCHAR(30), 
	macro_risk_score FLOAT, 
	macro_veto BOOLEAN, 
	PRIMARY KEY (id)
)
    """)
    op.execute("""
CREATE INDEX idx_agent_signals_agent_ts ON agent_signals (agent_name, timestamp)
    """)
    op.execute("""
CREATE INDEX idx_agent_signals_symbol_ts ON agent_signals (symbol, timestamp)
    """)
    op.execute("""
CREATE INDEX ix_agent_signals_agent_name ON agent_signals (agent_name)
    """)
    op.execute("""
CREATE INDEX ix_agent_signals_timestamp ON agent_signals (timestamp)
    """)
    op.execute("""
CREATE TABLE applied_strategy_changes (
	id SERIAL NOT NULL, 
	proposal_id VARCHAR(30) NOT NULL, 
	root_cause_category VARCHAR(50) NOT NULL, 
	tuning_parameter VARCHAR(100) NOT NULL, 
	tuning_direction VARCHAR(20) NOT NULL, 
	applied_value TEXT NOT NULL, 
	applied_by VARCHAR(20) NOT NULL, 
	notes TEXT NOT NULL, 
	backtest_delta_pf_pct FLOAT NOT NULL, 
	applied_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
)
    """)
    op.execute("""
CREATE INDEX idx_applied_applied_at ON applied_strategy_changes (applied_at)
    """)
    op.execute("""
CREATE INDEX idx_applied_param ON applied_strategy_changes (tuning_parameter)
    """)
    op.execute("""
CREATE TABLE exchanges (
	id SERIAL NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	exchange_id VARCHAR(50) NOT NULL, 
	api_key_encrypted TEXT, 
	api_secret_encrypted TEXT, 
	api_passphrase_encrypted TEXT, 
	sandbox_mode BOOLEAN NOT NULL, 
	demo_mode BOOLEAN NOT NULL, 
	is_active BOOLEAN NOT NULL, 
	testnet_url VARCHAR(255), 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
)
    """)
    op.execute("""
CREATE TABLE live_trades (
	id SERIAL NOT NULL, 
	symbol VARCHAR(30) NOT NULL, 
	side VARCHAR(5) NOT NULL, 
	status VARCHAR(10) NOT NULL, 
	regime VARCHAR(40) NOT NULL, 
	timeframe VARCHAR(10) NOT NULL, 
	entry_price FLOAT NOT NULL, 
	exit_price FLOAT, 
	stop_loss FLOAT, 
	take_profit FLOAT, 
	size_usdt FLOAT NOT NULL, 
	pnl_usdt FLOAT, 
	pnl_pct FLOAT, 
	score FLOAT NOT NULL, 
	exit_reason VARCHAR(30) NOT NULL, 
	models_fired JSONB, 
	rationale TEXT, 
	entry_order_id VARCHAR(80) NOT NULL, 
	exit_order_id VARCHAR(80) NOT NULL, 
	duration_s INTEGER NOT NULL, 
	opened_at VARCHAR(40) NOT NULL, 
	closed_at VARCHAR(40) NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
)
    """)
    op.execute("""
CREATE INDEX idx_live_trades_status ON live_trades (status)
    """)
    op.execute("""
CREATE INDEX idx_live_trades_closed_at ON live_trades (closed_at)
    """)
    op.execute("""
CREATE INDEX idx_live_trades_symbol ON live_trades (symbol)
    """)
    op.execute("""
CREATE TABLE paper_trades (
	id SERIAL NOT NULL, 
	symbol VARCHAR(30) NOT NULL, 
	side VARCHAR(5) NOT NULL, 
	regime VARCHAR(40) NOT NULL, 
	timeframe VARCHAR(10) NOT NULL, 
	entry_price FLOAT NOT NULL, 
	exit_price FLOAT NOT NULL, 
	stop_loss FLOAT, 
	take_profit FLOAT, 
	size_usdt FLOAT NOT NULL, 
	entry_size_usdt FLOAT, 
	exit_size_usdt FLOAT, 
	pnl_usdt FLOAT NOT NULL, 
	pnl_pct FLOAT NOT NULL, 
	score FLOAT NOT NULL, 
	exit_reason VARCHAR(30) NOT NULL, 
	models_fired JSONB, 
	rationale TEXT, 
	duration_s INTEGER NOT NULL, 
	opened_at VARCHAR(40) NOT NULL, 
	closed_at VARCHAR(40) NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
)
    """)
    op.execute("""
CREATE INDEX idx_paper_trades_symbol ON paper_trades (symbol)
    """)
    op.execute("""
CREATE INDEX idx_paper_trades_closed_at ON paper_trades (closed_at)
    """)
    op.execute("""
CREATE TABLE portfolio_snapshots (
	id SERIAL NOT NULL, 
	snapshot_type VARCHAR(10) NOT NULL, 
	timestamp TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	total_value FLOAT NOT NULL, 
	cash_balance FLOAT NOT NULL, 
	positions_value FLOAT NOT NULL, 
	unrealized_pnl FLOAT NOT NULL, 
	daily_pnl FLOAT NOT NULL, 
	drawdown FLOAT NOT NULL, 
	holdings JSONB, 
	PRIMARY KEY (id)
)
    """)
    op.execute("""
CREATE INDEX idx_portfolio_type_ts ON portfolio_snapshots (snapshot_type, timestamp)
    """)
    op.execute("""
CREATE TABLE settings (
	key VARCHAR(200) NOT NULL, 
	value TEXT, 
	category VARCHAR(50), 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (key)
)
    """)
    op.execute("""
CREATE TABLE signal_log (
	id SERIAL NOT NULL, 
	timestamp TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	symbol VARCHAR(30) NOT NULL, 
	strategy_name VARCHAR(80) NOT NULL, 
	direction VARCHAR(10) NOT NULL, 
	strength FLOAT NOT NULL, 
	entry_price FLOAT NOT NULL, 
	stop_loss FLOAT NOT NULL, 
	take_profit FLOAT NOT NULL, 
	regime VARCHAR(40), 
	timeframe VARCHAR(10), 
	rationale TEXT, 
	models_fired JSONB, 
	approved BOOLEAN NOT NULL, 
	rejection_reason VARCHAR(200), 
	PRIMARY KEY (id)
)
    """)
    op.execute("""
CREATE INDEX idx_signal_log_symbol_ts ON signal_log (symbol, timestamp)
    """)
    op.execute("""
CREATE INDEX ix_signal_log_symbol ON signal_log (symbol)
    """)
    op.execute("""
CREATE INDEX ix_signal_log_strategy_name ON signal_log (strategy_name)
    """)
    op.execute("""
CREATE INDEX ix_signal_log_timestamp ON signal_log (timestamp)
    """)
    op.execute("""
CREATE INDEX idx_signal_log_strategy ON signal_log (strategy_name, timestamp)
    """)
    op.execute("""
CREATE TABLE strategies (
	id SERIAL NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	description TEXT, 
	type VARCHAR(20) NOT NULL, 
	status VARCHAR(30) NOT NULL, 
	lifecycle_stage INTEGER NOT NULL, 
	definition JSONB, 
	ai_generated BOOLEAN NOT NULL, 
	ai_model_used VARCHAR(100), 
	created_by VARCHAR(50) NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
)
    """)
    op.execute("""
CREATE TABLE strategy_tuning_proposals (
	id SERIAL NOT NULL, 
	proposal_id VARCHAR(30) NOT NULL, 
	root_cause_category VARCHAR(50) NOT NULL, 
	rec_id VARCHAR(80) NOT NULL, 
	trigger_evidence JSONB, 
	affected_subsystem VARCHAR(100) NOT NULL, 
	tuning_parameter VARCHAR(100) NOT NULL, 
	tuning_direction VARCHAR(20) NOT NULL, 
	proposed_change_description TEXT NOT NULL, 
	expected_benefit TEXT NOT NULL, 
	confidence FLOAT NOT NULL, 
	risk_level VARCHAR(10) NOT NULL, 
	auto_tune_eligible BOOLEAN NOT NULL, 
	requires_manual_approval BOOLEAN NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	backtest_result JSONB, 
	promoted_at TIMESTAMP WITHOUT TIME ZONE, 
	rejected_at TIMESTAMP WITHOUT TIME ZONE, 
	rejection_reason TEXT, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (proposal_id)
)
    """)
    op.execute("""
CREATE INDEX idx_proposal_status ON strategy_tuning_proposals (status)
    """)
    op.execute("""
CREATE INDEX idx_proposal_root_cause ON strategy_tuning_proposals (root_cause_category)
    """)
    op.execute("""
CREATE INDEX idx_proposal_param ON strategy_tuning_proposals (tuning_parameter)
    """)
    op.execute("""
CREATE TABLE system_logs (
	id SERIAL NOT NULL, 
	timestamp TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	level VARCHAR(10) NOT NULL, 
	module VARCHAR(100) NOT NULL, 
	message TEXT NOT NULL, 
	details JSONB, 
	PRIMARY KEY (id)
)
    """)
    op.execute("""
CREATE INDEX idx_log_level_ts ON system_logs (level, timestamp)
    """)
    op.execute("""
CREATE TABLE trade_feedback (
	id SERIAL NOT NULL, 
	trade_id VARCHAR(100) NOT NULL, 
	symbol VARCHAR(30) NOT NULL, 
	side VARCHAR(5) NOT NULL, 
	regime VARCHAR(40) NOT NULL, 
	models_fired JSONB, 
	setup_score FLOAT NOT NULL, 
	risk_score FLOAT NOT NULL, 
	execution_score FLOAT NOT NULL, 
	decision_score FLOAT NOT NULL, 
	overall_score FLOAT NOT NULL, 
	classification VARCHAR(10) NOT NULL, 
	hard_overrides JSONB, 
	root_causes JSONB, 
	recommendations JSONB, 
	penalty_log JSONB, 
	ai_explanation TEXT, 
	pnl_usdt FLOAT NOT NULL, 
	pnl_pct FLOAT NOT NULL, 
	exit_reason VARCHAR(30) NOT NULL, 
	duration_s INTEGER NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	decision_outcome_matrix VARCHAR(40), 
	avoidable_loss_flag BOOLEAN, 
	avoidable_win_flag BOOLEAN, 
	was_loss_acceptable BOOLEAN, 
	failure_domain_primary VARCHAR(30), 
	failure_domain_secondary VARCHAR(30), 
	preventability_score FLOAT NOT NULL, 
	randomness_score FLOAT NOT NULL, 
	model_conflict_score FLOAT NOT NULL, 
	regime_confidence_at_entry FLOAT NOT NULL, 
	htf_confirmed_at_entry BOOLEAN, 
	signal_conflict_score FLOAT NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (trade_id)
)
    """)
    op.execute("""
CREATE INDEX idx_feedback_regime ON trade_feedback (regime)
    """)
    op.execute("""
CREATE INDEX idx_feedback_classification ON trade_feedback (classification)
    """)
    op.execute("""
CREATE INDEX idx_feedback_created_at ON trade_feedback (created_at)
    """)
    op.execute("""
CREATE INDEX idx_feedback_symbol ON trade_feedback (symbol)
    """)
    op.execute("""
CREATE TABLE tuning_proposal_outcomes (
	id SERIAL NOT NULL, 
	proposal_id VARCHAR(30) NOT NULL, 
	status VARCHAR(30) NOT NULL, 
	min_trades_threshold INTEGER NOT NULL, 
	pre_trades INTEGER NOT NULL, 
	pre_win_rate FLOAT, 
	pre_profit_factor FLOAT, 
	pre_avg_r FLOAT, 
	post_trades INTEGER NOT NULL, 
	post_win_rate FLOAT, 
	post_profit_factor FLOAT, 
	post_avg_r FLOAT, 
	delta_win_rate FLOAT, 
	delta_pf FLOAT, 
	verdict VARCHAR(20), 
	notes TEXT, 
	applied_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	measured_at TIMESTAMP WITHOUT TIME ZONE, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (proposal_id)
)
    """)
    op.execute("""
CREATE INDEX idx_tpo_proposal_id ON tuning_proposal_outcomes (proposal_id)
    """)
    op.execute("""
CREATE INDEX idx_tpo_status ON tuning_proposal_outcomes (status)
    """)
    op.execute("""
CREATE TABLE web_refresh_tokens (
	id SERIAL NOT NULL, 
	user_id INTEGER NOT NULL, 
	token_hash VARCHAR(255) NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	revoked BOOLEAN NOT NULL, 
	revoked_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	UNIQUE (token_hash)
)
    """)
    op.execute("""
CREATE INDEX ix_web_refresh_tokens_user_id ON web_refresh_tokens (user_id)
    """)
    op.execute("""
CREATE TABLE web_users (
	id SERIAL NOT NULL, 
	email VARCHAR(255) NOT NULL, 
	hashed_password TEXT NOT NULL, 
	is_active BOOLEAN NOT NULL, 
	is_admin BOOLEAN NOT NULL, 
	display_name VARCHAR(100), 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	last_login TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	UNIQUE (email)
)
    """)
    op.execute("""
CREATE TABLE assets (
	id SERIAL NOT NULL, 
	exchange_id INTEGER NOT NULL, 
	symbol VARCHAR(20) NOT NULL, 
	base_currency VARCHAR(10) NOT NULL, 
	quote_currency VARCHAR(10) NOT NULL, 
	min_amount FLOAT, 
	min_cost FLOAT, 
	price_precision INTEGER NOT NULL, 
	amount_precision INTEGER NOT NULL, 
	is_active BOOLEAN NOT NULL, 
	last_updated TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_asset_exchange_symbol UNIQUE (exchange_id, symbol), 
	FOREIGN KEY(exchange_id) REFERENCES exchanges (id)
)
    """)
    op.execute("""
CREATE TABLE backtest_results (
	id SERIAL NOT NULL, 
	strategy_id INTEGER, 
	strategy_name VARCHAR(200) NOT NULL, 
	symbol VARCHAR(20) NOT NULL, 
	timeframe VARCHAR(5) NOT NULL, 
	initial_capital FLOAT NOT NULL, 
	final_capital FLOAT NOT NULL, 
	total_return_pct FLOAT, 
	max_drawdown_pct FLOAT, 
	sharpe_ratio FLOAT, 
	win_rate FLOAT, 
	total_trades INTEGER NOT NULL, 
	profit_factor FLOAT, 
	run_config JSONB, 
	equity_curve JSONB, 
	trade_log JSONB, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(strategy_id) REFERENCES strategies (id)
)
    """)
    op.execute("""
CREATE INDEX idx_backtest_strategy_ts ON backtest_results (strategy_id, created_at)
    """)
    op.execute("""
CREATE TABLE strategy_metrics (
	id SERIAL NOT NULL, 
	strategy_id INTEGER NOT NULL, 
	run_type VARCHAR(20) NOT NULL, 
	period_start TIMESTAMP WITHOUT TIME ZONE, 
	period_end TIMESTAMP WITHOUT TIME ZONE, 
	total_trades INTEGER NOT NULL, 
	win_rate FLOAT, 
	profit_factor FLOAT, 
	sharpe_ratio FLOAT, 
	sortino_ratio FLOAT, 
	max_drawdown FLOAT, 
	total_pnl FLOAT, 
	total_pnl_pct FLOAT, 
	avg_trade_duration_hrs FLOAT, 
	calculated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(strategy_id) REFERENCES strategies (id)
)
    """)
    op.execute("""
CREATE TABLE features (
	id SERIAL NOT NULL, 
	asset_id INTEGER NOT NULL, 
	timeframe VARCHAR(5) NOT NULL, 
	timestamp TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	feature_name VARCHAR(100) NOT NULL, 
	value FLOAT, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_feature UNIQUE (asset_id, timeframe, timestamp, feature_name), 
	FOREIGN KEY(asset_id) REFERENCES assets (id)
)
    """)
    op.execute("""
CREATE INDEX idx_feature_asset_tf_ts ON features (asset_id, timeframe, timestamp)
    """)
    op.execute("""
CREATE TABLE market_regimes (
	id SERIAL NOT NULL, 
	asset_id INTEGER NOT NULL, 
	timeframe VARCHAR(5) NOT NULL, 
	timestamp TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	regime VARCHAR(30) NOT NULL, 
	confidence FLOAT, 
	features JSONB, 
	PRIMARY KEY (id), 
	FOREIGN KEY(asset_id) REFERENCES assets (id)
)
    """)
    op.execute("""
CREATE INDEX idx_regime_asset_tf_ts ON market_regimes (asset_id, timeframe, timestamp)
    """)
    op.execute("""
CREATE TABLE ml_models (
	id SERIAL NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	model_type VARCHAR(50) NOT NULL, 
	asset_id INTEGER, 
	timeframe VARCHAR(5), 
	version INTEGER NOT NULL, 
	accuracy FLOAT, 
	feature_importance JSONB, 
	model_path VARCHAR(500), 
	trained_at TIMESTAMP WITHOUT TIME ZONE, 
	valid_until TIMESTAMP WITHOUT TIME ZONE, 
	is_active BOOLEAN NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(asset_id) REFERENCES assets (id)
)
    """)
    op.execute("""
CREATE TABLE ohlcv (
	id SERIAL NOT NULL, 
	asset_id INTEGER NOT NULL, 
	timeframe VARCHAR(5) NOT NULL, 
	timestamp TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	open FLOAT NOT NULL, 
	high FLOAT NOT NULL, 
	low FLOAT NOT NULL, 
	close FLOAT NOT NULL, 
	volume FLOAT NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_ohlcv UNIQUE (asset_id, timeframe, timestamp), 
	FOREIGN KEY(asset_id) REFERENCES assets (id)
)
    """)
    op.execute("""
CREATE INDEX idx_ohlcv_asset_tf_ts ON ohlcv (asset_id, timeframe, timestamp)
    """)
    op.execute("""
CREATE TABLE positions (
	id SERIAL NOT NULL, 
	strategy_id INTEGER, 
	exchange_id INTEGER NOT NULL, 
	asset_id INTEGER NOT NULL, 
	position_type VARCHAR(10) NOT NULL, 
	side VARCHAR(5) NOT NULL, 
	entry_price FLOAT NOT NULL, 
	current_price FLOAT NOT NULL, 
	quantity FLOAT NOT NULL, 
	unrealized_pnl FLOAT NOT NULL, 
	stop_loss FLOAT, 
	take_profit FLOAT, 
	is_open BOOLEAN NOT NULL, 
	opened_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(strategy_id) REFERENCES strategies (id), 
	FOREIGN KEY(exchange_id) REFERENCES exchanges (id), 
	FOREIGN KEY(asset_id) REFERENCES assets (id)
)
    """)
    op.execute("""
CREATE TABLE sentiment_data (
	id SERIAL NOT NULL, 
	asset_id INTEGER, 
	source VARCHAR(20) NOT NULL, 
	timestamp TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	sentiment_score FLOAT, 
	narrative_score FLOAT, 
	attention_index FLOAT, 
	raw_data JSONB, 
	PRIMARY KEY (id), 
	FOREIGN KEY(asset_id) REFERENCES assets (id)
)
    """)
    op.execute("""
CREATE INDEX idx_sentiment_asset_ts ON sentiment_data (asset_id, timestamp)
    """)
    op.execute("""
CREATE TABLE signals (
	id SERIAL NOT NULL, 
	strategy_id INTEGER NOT NULL, 
	asset_id INTEGER NOT NULL, 
	signal_type VARCHAR(20) NOT NULL, 
	timestamp TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	price FLOAT NOT NULL, 
	confidence FLOAT, 
	regime VARCHAR(30), 
	timeframe_alignment JSONB, 
	indicator_values JSONB, 
	ai_prediction JSONB, 
	sentiment_score FLOAT, 
	microstructure_score FLOAT, 
	status VARCHAR(20) NOT NULL, 
	rejection_reason TEXT, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(strategy_id) REFERENCES strategies (id), 
	FOREIGN KEY(asset_id) REFERENCES assets (id)
)
    """)
    op.execute("""
CREATE INDEX idx_signal_strategy_ts ON signals (strategy_id, timestamp)
    """)
    op.execute("""
CREATE TABLE model_predictions (
	id SERIAL NOT NULL, 
	model_id INTEGER NOT NULL, 
	asset_id INTEGER NOT NULL, 
	timestamp TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	bullish_probability FLOAT, 
	bearish_probability FLOAT, 
	expected_return FLOAT, 
	confidence FLOAT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(model_id) REFERENCES ml_models (id), 
	FOREIGN KEY(asset_id) REFERENCES assets (id)
)
    """)
    op.execute("""
CREATE INDEX idx_prediction_model_ts ON model_predictions (model_id, timestamp)
    """)
    op.execute("""
CREATE TABLE trades (
	id SERIAL NOT NULL, 
	strategy_id INTEGER, 
	signal_id INTEGER, 
	asset_id INTEGER NOT NULL, 
	exchange_id INTEGER NOT NULL, 
	trade_type VARCHAR(20) NOT NULL, 
	side VARCHAR(5) NOT NULL, 
	entry_time TIMESTAMP WITHOUT TIME ZONE, 
	exit_time TIMESTAMP WITHOUT TIME ZONE, 
	entry_price FLOAT NOT NULL, 
	exit_price FLOAT, 
	quantity FLOAT NOT NULL, 
	pnl FLOAT, 
	pnl_pct FLOAT, 
	fees FLOAT NOT NULL, 
	slippage FLOAT NOT NULL, 
	exit_reason VARCHAR(50), 
	explanation JSONB, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(strategy_id) REFERENCES strategies (id), 
	FOREIGN KEY(signal_id) REFERENCES signals (id), 
	FOREIGN KEY(asset_id) REFERENCES assets (id), 
	FOREIGN KEY(exchange_id) REFERENCES exchanges (id)
)
    """)
    op.execute("""
CREATE INDEX idx_trade_strategy ON trades (strategy_id)
    """)
    op.execute("""
CREATE INDEX idx_trade_type_ts ON trades (trade_type, entry_time)
    """)
    op.execute("""
CREATE TABLE orders (
	id SERIAL NOT NULL, 
	trade_id INTEGER, 
	exchange_id INTEGER NOT NULL, 
	exchange_order_id VARCHAR(100), 
	symbol VARCHAR(20) NOT NULL, 
	order_type VARCHAR(20) NOT NULL, 
	side VARCHAR(5) NOT NULL, 
	price FLOAT, 
	amount FLOAT NOT NULL, 
	filled FLOAT NOT NULL, 
	remaining FLOAT NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	timestamp TIMESTAMP WITHOUT TIME ZONE, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(trade_id) REFERENCES trades (id), 
	FOREIGN KEY(exchange_id) REFERENCES exchanges (id)
)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS orders CASCADE")
    op.execute("DROP TABLE IF EXISTS trades CASCADE")
    op.execute("DROP TABLE IF EXISTS model_predictions CASCADE")
    op.execute("DROP TABLE IF EXISTS signals CASCADE")
    op.execute("DROP TABLE IF EXISTS sentiment_data CASCADE")
    op.execute("DROP TABLE IF EXISTS positions CASCADE")
    op.execute("DROP TABLE IF EXISTS ohlcv CASCADE")
    op.execute("DROP TABLE IF EXISTS ml_models CASCADE")
    op.execute("DROP TABLE IF EXISTS market_regimes CASCADE")
    op.execute("DROP TABLE IF EXISTS features CASCADE")
    op.execute("DROP TABLE IF EXISTS strategy_metrics CASCADE")
    op.execute("DROP TABLE IF EXISTS backtest_results CASCADE")
    op.execute("DROP TABLE IF EXISTS assets CASCADE")
    op.execute("DROP TABLE IF EXISTS web_users CASCADE")
    op.execute("DROP TABLE IF EXISTS web_refresh_tokens CASCADE")
    op.execute("DROP TABLE IF EXISTS tuning_proposal_outcomes CASCADE")
    op.execute("DROP TABLE IF EXISTS trade_feedback CASCADE")
    op.execute("DROP TABLE IF EXISTS system_logs CASCADE")
    op.execute("DROP TABLE IF EXISTS strategy_tuning_proposals CASCADE")
    op.execute("DROP TABLE IF EXISTS strategies CASCADE")
    op.execute("DROP TABLE IF EXISTS signal_log CASCADE")
    op.execute("DROP TABLE IF EXISTS settings CASCADE")
    op.execute("DROP TABLE IF EXISTS portfolio_snapshots CASCADE")
    op.execute("DROP TABLE IF EXISTS paper_trades CASCADE")
    op.execute("DROP TABLE IF EXISTS live_trades CASCADE")
    op.execute("DROP TABLE IF EXISTS exchanges CASCADE")
    op.execute("DROP TABLE IF EXISTS applied_strategy_changes CASCADE")
    op.execute("DROP TABLE IF EXISTS agent_signals CASCADE")
