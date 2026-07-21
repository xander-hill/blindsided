import os

from blindsided.generated import blindsided_pb2 as pb2


class StorageSnapshotRepository:
    """Serialize storage snapshots using an atomic filesystem replacement."""

    def __init__(self, state_file_path: str) -> None:
        self.state_file_path = state_file_path

    def load(self) -> pb2.StorageSnapshot | None:
        """Return the serialized snapshot, or None when persistence is disabled."""
        if not self.state_file_path or not os.path.exists(self.state_file_path):
            return None

        snapshot = pb2.StorageSnapshot()
        with open(self.state_file_path, "rb") as state_file:
            snapshot.ParseFromString(state_file.read())
        return snapshot

    def save(self, snapshot: pb2.StorageSnapshot) -> None:
        """Atomically replace the active snapshot with serialized state."""
        if not self.state_file_path:
            return

        state_dir = os.path.dirname(self.state_file_path)
        if state_dir:
            os.makedirs(state_dir, exist_ok=True)
        # Replacement happens only after the complete snapshot reaches the
        # temporary file, so readers never observe a partially written payload.
        temp_path = f"{self.state_file_path}.tmp"
        with open(temp_path, "wb") as state_file:
            state_file.write(snapshot.SerializeToString())
        os.replace(temp_path, self.state_file_path)
