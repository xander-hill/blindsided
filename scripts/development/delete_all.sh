#!/bin/bash
echo "🗑️ Tearing down marketplace..."
kubectl delete -f deploy/envoy/kubernetes.yaml --ignore-not-found
kubectl delete -f deploy/kubernetes/service.yaml --ignore-not-found
kubectl delete -f deploy/kubernetes/storage.yaml --ignore-not-found
kubectl delete -f deploy/kubernetes/controller.yaml --ignore-not-found
echo "✨ Cleaned up!"
