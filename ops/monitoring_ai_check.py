"""Real local inference check using a disposable SQLite database, never plant data."""
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from app.database import RobotCommand
from app.monitoring import Monitoring
from app.schemas import RobotConfig, RobotMeasurement
from app.service import Service
from sqlalchemy import func, select


def main():
    os.environ['MONITORING_AI_ENABLED'] = 'true'
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='allur-monitoring-check-') as directory:
        service = Service('sqlite:///' + str(Path(directory) / 'isolated.db'))
        monitor = Monitoring(service)
        try:
            reading = RobotMeasurement(robot_id='ISOLATED-AI-CHECK', timestamp=datetime.now(timezone.utc).isoformat(),
                line_section='welding', operation='welding', cycle_status='running',
                joint_temperature_c=85, vibration_mm_s=2, controller_mode='automatic', safety_state='normal')
            service.ingest_measurements([reading.model_dump()])
            service.configure_robot(reading.robot_id, RobotConfig(expected_interval_seconds=60,
                limits={'joint_temperature_c': {'high_warning': 60, 'high_critical': 80}}).model_dump())
            monitor.scan_once()
            assert monitor.snapshot()['active_count'] == 1
            assert monitor.process_ai_once()
            alert = monitor.snapshot()['alerts'][0]
            assert alert['ai_status'] == 'ready', alert.get('ai_error')
            assert alert['ai_analysis'] and alert['ai_actions']
            with service.sessions() as db:
                assert db.scalar(select(func.count()).select_from(RobotCommand)) == 0
            print(json.dumps({'status': 'passed', 'seconds': round(time.monotonic() - started, 1),
                'model': os.getenv('AI_MODEL', 'qwen3:4b'), 'analysis': alert['ai_analysis'],
                'actions': alert['ai_actions'], 'physical_commands': 0, 'database': 'disposable SQLite'}, ensure_ascii=False))
        finally:
            monitor.close()
            service.close()


if __name__ == '__main__':
    main()
