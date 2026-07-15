import re
from pathlib import Path

from backend.tests.helpers import BackendTestCase


ROOT = Path(__file__).resolve().parents[3]


def manifest_text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text()


def manifest_doc(relative_path: str, *, kind: str, name: str) -> str:
    docs = manifest_text(relative_path).split("---")
    for doc in docs:
        if re.search(rf"^kind:\s*{re.escape(kind)}\s*$", doc, re.MULTILINE) and re.search(
            rf"^\s*name:\s*{re.escape(name)}\s*$", doc, re.MULTILINE
        ):
            return doc
    raise AssertionError(f"{kind}/{name} not found in {relative_path}")


def int_field(doc: str, field: str) -> int:
    match = re.search(rf"^\s*{re.escape(field)}:\s*(\d+)\s*$", doc, re.MULTILINE)
    if not match:
        raise AssertionError(f"{field} not found in manifest document:\n{doc}")
    return int(match.group(1))


class KubernetesScalingAndReplicationManifestTests(BackendTestCase):
    def test_service_node_deployment_is_scaled_and_fronted_by_matching_service(self):
        deployment = manifest_doc(
            "deploy/kubernetes/service.yaml",
            kind="Deployment",
            name="service-node",
        )
        service = manifest_doc(
            "deploy/kubernetes/service.yaml",
            kind="Service",
            name="service-nodes",
        )

        self.assertGreaterEqual(int_field(deployment, "replicas"), 2)
        self.assertIn('command: ["python", "-m", "blindsided.auction_service.server"]', deployment)
        self.assertRegex(deployment, r"matchLabels:\s*\n\s*app:\s*service-node")
        self.assertRegex(service, r"selector:\s*\n\s*app:\s*service-node")
        self.assertRegex(service, r"port:\s*50051")
        self.assertRegex(service, r"targetPort:\s*50051")

    def test_storage_statefulset_is_replicated_and_uses_stable_peer_dns(self):
        service = manifest_doc(
            "deploy/kubernetes/storage.yaml",
            kind="Service",
            name="storage-service",
        )
        statefulset = manifest_doc(
            "deploy/kubernetes/storage.yaml",
            kind="StatefulSet",
            name="storage",
        )

        replicas = int_field(statefulset, "replicas")
        self.assertGreaterEqual(replicas, 2)
        self.assertRegex(service, r"clusterIP:\s*None")
        self.assertRegex(statefulset, r'serviceName:\s*"storage-service"')
        self.assertIn('command: ["python", "-m", "blindsided.storage.server"]', statefulset)

        peer_match = re.search(r'name:\s*PEER_ADDRESSES\s*\n\s*value:\s*"([^"]+)"', statefulset)
        self.assertIsNotNone(peer_match)
        peers = peer_match.group(1).split(",")

        self.assertEqual(len(peers), replicas)
        self.assertEqual(
            peers,
            [f"storage-{index}.storage-service:50051" for index in range(replicas)],
        )

    def test_storage_pod_identity_comes_from_statefulset_pod_name(self):
        statefulset = manifest_doc(
            "deploy/kubernetes/storage.yaml",
            kind="StatefulSet",
            name="storage",
        )

        self.assertRegex(statefulset, r"name:\s*POD_IP\s*\n\s*valueFrom:")
        self.assertRegex(statefulset, r"fieldPath:\s*metadata.name")

    def test_storage_statefulset_persists_local_snapshot_across_restarts(self):
        statefulset = manifest_doc(
            "deploy/kubernetes/storage.yaml",
            kind="StatefulSet",
            name="storage",
        )

        self.assertRegex(statefulset, r"name:\s*AUCTION_STORE_PATH")
        self.assertIn("value: \"/var/lib/blindsided/auction-state.pb\"", statefulset)
        self.assertRegex(statefulset, r"name:\s*storage-state\s*\n\s*mountPath:\s*/var/lib/blindsided")
        self.assertRegex(statefulset, r"volumeClaimTemplates:")
        self.assertRegex(statefulset, r'accessModes:\s*\["ReadWriteOnce"\]')
