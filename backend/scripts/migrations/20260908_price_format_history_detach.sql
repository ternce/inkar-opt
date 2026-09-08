ALTER TABLE price_lists ALTER COLUMN price_format_id DROP NOT NULL;
ALTER TABLE pricing_workflow_runs ALTER COLUMN price_format_id DROP NOT NULL;
