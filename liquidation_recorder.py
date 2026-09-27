import json
import os
import logging
import time
from datetime import datetime, timedelta

class LiquidationRecorder:
    def __init__(self, storage_file="data/okx_liquidations.jsonl", logger=None):
        self.storage_file = storage_file
        self.logger = logger or logging.getLogger(__name__)
        
        # Ensure data directory exists
        os.makedirs(os.path.dirname(self.storage_file), exist_ok=True)
        
        # Cache existing timestamps for fast deduplication
        self.existing_timestamps = self._load_existing_timestamps()
        self.logger.info(f"LiquidationRecorder initialized. {len(self.existing_timestamps)} events in history.")

    def _load_existing_timestamps(self):
        """Load all timestamps from the file to avoid duplicates."""
        timestamps = set()
        if os.path.exists(self.storage_file):
            try:
                with open(self.storage_file, 'r') as f:
                    for line in f:
                        try:
                            data = json.loads(line)
                            if 'ts' in data:
                                timestamps.add(data['ts'])
                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                self.logger.error(f"Error loading liquidation history: {e}")
        return timestamps

    def save_liquidations(self, liquidations: list):
        """Save new unique liquidations to the JSONL file."""
        if not liquidations:
            return
            
        new_items = []
        for liq in liquidations:
            ts = liq.get('ts')
            if ts and ts not in self.existing_timestamps:
                new_items.append(liq)
                self.existing_timestamps.add(ts)
        
        if new_items:
            try:
                with open(self.storage_file, 'a') as f:
                    for item in new_items:
                        f.write(json.dumps(item) + '\n')
                self.logger.info(f"Saved {len(new_items)} new liquidation events to {self.storage_file}")
            except Exception as e:
                self.logger.error(f"Error saving liquidations: {e}")
        else:
            self.logger.debug("No new unique liquidations to save.")

    def load_history(self, days=180):
        """Load historical liquidations within the specified number of days."""
        cutoff_ts = int((datetime.now() - timedelta(days=days)).timestamp() * 1000)
        history = []
        
        if os.path.exists(self.storage_file):
            try:
                with open(self.storage_file, 'r') as f:
                    for line in f:
                        try:
                            data = json.loads(line)
                            if data.get('ts', 0) >= cutoff_ts:
                                history.append(data)
                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                self.logger.error(f"Error reading history: {e}")
                
        return sorted(history, key=lambda x: x['ts'])

if __name__ == "__main__":
    # Quick self-test
    logging.basicConfig(level=logging.INFO)
    recorder = LiquidationRecorder(storage_file="data/test_liquidations.jsonl")
    
    now_ms = int(time.time() * 1000)
    test_data = [
        {"ts": now_ms - 5000, "bkPx": 2000.0, "sz": 10.0, "side": "buy"},
        {"ts": now_ms - 2000, "bkPx": 2010.0, "sz": 5.0, "side": "sell"},
        {"ts": now_ms - 5000, "bkPx": 2000.0, "sz": 10.0, "side": "buy"} # Duplicate
    ]
    
    recorder.save_liquidations(test_data)
    history = recorder.load_history(days=1)
    print(f"Loaded history: {len(history)} items")
    assert len(history) == 2
    
    # Cleanup
    if os.path.exists("data/test_liquidations.jsonl"):
        os.remove("data/test_liquidations.jsonl")
    print("Self-test passed!")
