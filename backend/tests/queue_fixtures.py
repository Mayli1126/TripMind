from datetime import datetime, timezone
from app.infrastructure import redis_client
from app.services.planning_metrics_service import new_metrics


def publish_fixture(job_id, job_type, payload):
    now=datetime.now(timezone.utc)
    return redis_client.publish_job(job_id,job_type,payload,dict(job_type=job_type,status='queued',
        stage='queued',progress=0,agents={},metrics=new_metrics(now),created_at=now.isoformat(),
        updated_at=now.isoformat(),request=payload.get('request')))
