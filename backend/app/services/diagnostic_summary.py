"""Bounded event summaries. Counts are events, not billing or exact HTTP requests."""
def summarize_event(data, event):
    operations = data.setdefault('operations', {})
    key = f"{event.get('service','runtime')}:{event.get('operation','unknown')}"[:160]
    if key not in operations and len(operations) >= 128:
        key = 'other'
    row = operations.setdefault(key, dict(events=0, statuses={}, duration_ms=0, hits=0, misses=0,
                                           http_429=0, timeouts=0))
    row['events'] += 1
    status = str(event.get('status', 'unknown'))[:32]
    if status not in row['statuses'] and len(row['statuses']) >= 32:
        status = 'other'
    row['statuses'][status] = row['statuses'].get(status, 0)+1
    row['duration_ms'] += max(0, event.get('duration_ms', 0))
    if 'hit' in event:
        row['hits' if event['hit'] else 'misses'] += 1
    if event.get('http_status') == 429:
        row['http_429'] += 1
    if 'timeout' in str(event.get('error_type', '')).lower() or status == 'timeout':
        row['timeouts'] += 1
