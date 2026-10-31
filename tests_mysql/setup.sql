-- CI only: run by .github/workflows/ci.yml as root inside the disposable MySQL
-- service container, before docs/v2-live-validation/02-fixture.sql loads the
-- synthetic rows. Never run it against any other server.
--
-- The passwords are test-only values for that container, which exists for one
-- job and is published on the runner's 127.0.0.1 only. They are repeated in
-- tests_mysql/live_mysql_checks.py.
--
-- Accounts use host '%' because connections from the runner reach the
-- container through Docker's port mapping, not from 127.0.0.1 inside it.

CREATE SCHEMA validator_live_test;

-- A schema the evidence account has no privilege on (permission-failure test).
CREATE SCHEMA validator_ci_private;
CREATE TABLE validator_ci_private.hidden (id INT PRIMARY KEY);

-- The validator's evidence account: SELECT on the test schema and nothing else.
CREATE USER 'vt_evidence'@'%' IDENTIFIED BY 'ci-only-evidence-pw-1';
GRANT SELECT ON validator_live_test.* TO 'vt_evidence'@'%';

-- Negative safety test only: a login that COULD write, to show the validator
-- still sends no submitted write. Never used for anything else.
CREATE USER 'vt_writer'@'%' IDENTIFIED BY 'ci-only-writer-pw-1';
GRANT SELECT, INSERT, UPDATE, DELETE ON validator_live_test.* TO 'vt_writer'@'%';

-- Observer: reads statement digests (which statements reached the server).
CREATE USER 'vt_observer'@'%' IDENTIFIED BY 'ci-only-observer-pw-1';
GRANT SELECT ON performance_schema.events_statements_summary_by_digest TO 'vt_observer'@'%';
