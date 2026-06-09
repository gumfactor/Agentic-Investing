-- Runs once at container first boot (docker-entrypoint-initdb.d).
-- Creates the secondary databases needed by Airflow and MLflow.
-- The primary 'rqis' database is already created by POSTGRES_DB env var.

CREATE DATABASE airflow;
CREATE DATABASE mlflow;

GRANT ALL PRIVILEGES ON DATABASE airflow TO rqis;
GRANT ALL PRIVILEGES ON DATABASE mlflow TO rqis;
