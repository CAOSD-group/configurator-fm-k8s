#!/usr/bin/env python3
"""Generate LLM benchmark prompt cases from Kubernetes FM variation-point profiles.

Input format expected (minimum):
{
  "gvk": {"apiVersion": "apps/v1", "kind": "Deployment"},
  "points": [
    {
      "case_id": "...",
      "point_feature": "...",
      "variants": ["..."],
      "config_path": "spec....",
      "plan_type": "...",
      "n_array_ancestors": 0,
      "status": "planned"
    }
  ]
}

The script deliberately does NOT put alternative values in the prompt.  It asks
only for the variation point, matching the FM experiment where the point is
selected and the solver/model is allowed to choose an alternative.

No third-party Python packages are required.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

FORMAT_VERSION = "k8s_llm_prompt_cases_v2"

# Fields that are normally API/server bookkeeping rather than declarative user
# configuration. These rules are intentionally conservative and path-based.
SYSTEM_MANAGED_SEGMENTS = {
    "managedFields",
    "resourceVersion",
    "uid",
    "generation",
    "creationTimestamp",
    "deletionTimestamp",
    "deletionGracePeriodSeconds",
    "selfLink",
}

# Segment display names used by both semantic rules and the generic fallback.
SEGMENT_LABELS = {
    "apiVersion": "API version",
    "appArmorProfile": "AppArmor profile",
    "awsElasticBlockStore": "AWS Elastic Block Store",
    "azureDisk": "Azure Disk",
    "bindMountOptions": "bind-mount options",
    "cachingMode": "caching mode",
    "clusterTrustBundle": "cluster trust bundle",
    "dnsPolicy": "DNS policy",
    "downwardAPI": "Downward API",
    "emptyDir": "emptyDir volume",
    "ephemeral": "generic ephemeral volume",
    "ephemeralContainers": "ephemeral containers",
    "exitCodes": "exit codes",
    "fc": "Fibre Channel volume",
    "flexVolume": "FlexVolume",
    "fsGroupChangePolicy": "filesystem group change policy",
    "fsType": "filesystem type",
    "gcePersistentDisk": "GCE persistent disk",
    "httpGet": "HTTP GET",
    "imagePullPolicy": "image pull policy",
    "initContainers": "init containers",
    "iscsi": "iSCSI volume",
    "keyType": "key type",
    "livenessProbe": "liveness probe",
    "localhostProfile": "local profile",
    "matchExpressions": "match expressions",
    "matchFields": "field matches",
    "maxSurge": "maximum surge",
    "maxUnavailable": "maximum unavailable replicas",
    "nodeAffinity": "node affinity",
    "nodeSelectorTerms": "node selector terms",
    "os": "operating system",
    "overhead": "overhead resources",
    "photonPersistentDisk": "Photon persistent disk",
    "podAffinity": "pod affinity",
    "podAntiAffinity": "pod anti-affinity",
    "podCertificate": "Pod certificate",
    "portworxVolume": "Portworx volume",
    "preemptionPolicy": "preemption policy",
    "preStop": "pre-stop hook",
    "postStart": "post-start hook",
    "projected": "projected volume",
    "readinessProbe": "readiness probe",
    "resourceFieldRef": "resource field reference",
    "resourceName": "resource name",
    "restartPolicy": "restart policy",
    "restartPolicyRules": "restart policy rules",
    "rbd": "RBD volume",
    "scaleIO": "ScaleIO volume",
    "seLinuxChangePolicy": "SELinux change policy",
    "seccompProfile": "seccomp profile",
    "securityContext": "security context",
    "sizeLimit": "size limit",
    "startupProbe": "startup probe",
    "storageMode": "storage mode",
    "storageos": "StorageOS volume",
    "supplementalGroupsPolicy": "supplemental groups policy",
    "tcpSocket": "TCP socket",
    "topologySpreadConstraints": "topology spread constraints",
    "volumeClaimTemplate": "volume claim template",
    "volumeMounts": "volume mounts",
    "vsphereVolume": "vSphere volume",
}

VOLUME_SOURCE_LABELS = {
    "awsElasticBlockStore": "an AWS Elastic Block Store volume",
    "azureDisk": "an Azure Disk volume",
    "cinder": "a Cinder volume",
    "fc": "a Fibre Channel volume",
    "flexVolume": "a FlexVolume volume",
    "gcePersistentDisk": "a GCE persistent disk volume",
    "iscsi": "an iSCSI volume",
    "photonPersistentDisk": "a Photon persistent disk volume",
    "portworxVolume": "a Portworx volume",
    "rbd": "an RBD volume",
    "scaleIO": "a ScaleIO volume",
    "storageos": "a StorageOS volume",
    "vsphereVolume": "a vSphere volume",
}

CONTAINER_SCOPE_LABELS = {
    "containers": "one of its containers",
    "initContainers": "one of its init containers",
    "ephemeralContainers": "one of its ephemeral containers",
}


@dataclass
class MappingResult:
    phrase: str
    method: str
    confidence: str


@dataclass
class CaseClassification:
    prompt_status: str       # READY | REVIEW | SKIP
    benchmark_group: str     # PRIMARY | EDGE | UNASSIGNED
    classification: str      # user_configurable, server_managed, ...
    reason: str


def split_camel(name: str) -> list[str]:
    """Split camelCase/PascalCase/acronym-like identifiers into words."""
    if not name:
        return []
    # Keep known Kubernetes-ish acronyms sensible through label overrides first.
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)
    text = text.replace("_", " ").replace("-", " ")
    return [part for part in text.split() if part]


def human_segment(segment: str) -> str:
    segment = segment.replace("[]", "")
    if segment in SEGMENT_LABELS:
        return SEGMENT_LABELS[segment]
    words = split_camel(segment)
    if not words:
        return segment
    # Lowercase ordinary words while preserving obvious all-caps acronyms.
    rendered = [w if w.isupper() and len(w) > 1 else w.lower() for w in words]
    return " ".join(rendered)


def path_segments(path: str) -> list[str]:
    return [p for p in path.split(".") if p]


def bare_segment(segment: str) -> str:
    return segment[:-2] if segment.endswith("[]") else segment


def load_overrides(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"exact": {}}
    if not path.exists():
        raise FileNotFoundError(f"Overrides file not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Overrides JSON must be an object.")
    data.setdefault("exact", {})
    return data


def exact_override(path: str, overrides: dict[str, Any]) -> MappingResult | None:
    item = overrides.get("exact", {}).get(path)
    if item is None:
        return None
    if isinstance(item, str):
        return MappingResult(item, "exact_override", "high")
    if isinstance(item, dict) and item.get("phrase"):
        return MappingResult(str(item["phrase"]), "exact_override", str(item.get("confidence", "high")))
    raise ValueError(f"Invalid override for {path!r}: expected string or object with 'phrase'.")


def classify_case(point: dict[str, Any]) -> CaseClassification:
    """Classify a variation point independently from prompt generation quality.

    PRIMARY cases are intended for the main comparison of user-configurable
    manifest variability. EDGE cases are still promptable and should be kept in
    the dataset, but analysed separately because their declarative semantics are
    special (for example server-managed metadata or ephemeral containers).

    SKIP is reserved for source cases that were not planned by the upstream
    profile. It is not used to silently discard semantically unusual points.
    """
    path = str(point.get("config_path", ""))
    status = str(point.get("status", "planned"))
    segments = [bare_segment(s) for s in path_segments(path)]

    if status != "planned":
        return CaseClassification(
            prompt_status="SKIP",
            benchmark_group="UNASSIGNED",
            classification="source_not_planned",
            reason=f"source_status_{status}",
        )

    if segments and segments[0] == "status":
        return CaseClassification(
            prompt_status="READY",
            benchmark_group="EDGE",
            classification="status_managed",
            reason="status_is_server_managed",
        )

    if any(seg in SYSTEM_MANAGED_SEGMENTS for seg in segments):
        return CaseClassification(
            prompt_status="READY",
            benchmark_group="EDGE",
            classification="server_managed",
            reason="system_managed_metadata",
        )

    # Ephemeral containers are updated through the Pod ephemeralcontainers
    # subresource rather than as ordinary declarative creation fields. We still
    # generate the prompt so FM and LLM behaviour can be compared, but keep it
    # out of the PRIMARY denominator.
    if "ephemeralContainers" in segments:
        return CaseClassification(
            prompt_status="READY",
            benchmark_group="EDGE",
            classification="non_declarative_context",
            reason="ephemeral_containers_subresource",
        )

    return CaseClassification(
        prompt_status="READY",
        benchmark_group="PRIMARY",
        classification="user_configurable",
        reason="",
    )


def strip_pod_prefix(path: str, kind: str) -> tuple[str, bool]:
    """Return (relative_path, is_pod_scope)."""
    prefixes = [
        "spec.template.spec.",
        "spec.jobTemplate.spec.template.spec.",
    ]
    for prefix in prefixes:
        if path.startswith(prefix):
            return path[len(prefix):], True
    if kind == "Pod" and path.startswith("spec."):
        return path[len("spec."):], True
    return path, False


def humanize_affinity(relative: str) -> MappingResult | None:
    # Node affinity.
    m = re.fullmatch(
        r"affinity\.nodeAffinity\.(preferredDuringSchedulingIgnoredDuringExecution\[\]|requiredDuringSchedulingIgnoredDuringExecution)"
        r"(?:\.preference|\.nodeSelectorTerms\[\])\.(matchExpressions\[\]|matchFields\[\])\.operator",
        relative,
    )
    if m:
        strength = "preferred" if m.group(1).startswith("preferred") else "required"
        match_kind = "match expression" if m.group(2).startswith("matchExpressions") else "field-matching expression"
        return MappingResult(
            f"the operator of a {match_kind} in its {strength} node affinity",
            "affinity_rule",
            "high",
        )

    # Pod affinity / anti-affinity, including labelSelector and namespaceSelector.
    m = re.fullmatch(
        r"affinity\.(podAffinity|podAntiAffinity)\.(preferredDuringSchedulingIgnoredDuringExecution\[\]|requiredDuringSchedulingIgnoredDuringExecution\[\])"
        r"(?:\.podAffinityTerm)?\.(labelSelector|namespaceSelector)\.matchExpressions\[\]\.operator",
        relative,
    )
    if m:
        affinity = "pod affinity" if m.group(1) == "podAffinity" else "pod anti-affinity"
        strength = "preferred" if m.group(2).startswith("preferred") else "required"
        selector = "label-selector" if m.group(3) == "labelSelector" else "namespace-selector"
        return MappingResult(
            f"the operator of a {selector} match expression in its {strength} {affinity}",
            "affinity_rule",
            "high",
        )

    return None


def humanize_container_subpath(subpath: str, scope: str) -> MappingResult | None:
    scope_label = CONTAINER_SCOPE_LABELS[scope]

    if subpath == "env[].valueFrom.resourceFieldRef.divisor":
        return MappingResult(
            f"the resource field reference divisor in an environment variable of {scope_label}",
            "container_rule",
            "high",
        )
    if subpath == "imagePullPolicy":
        return MappingResult(f"the image pull policy of {scope_label}", "container_rule", "high")

    m = re.fullmatch(r"lifecycle\.(postStart|preStop)\.(httpGet|tcpSocket)\.port", subpath)
    if m:
        hook = "post-start" if m.group(1) == "postStart" else "pre-stop"
        transport = "HTTP GET" if m.group(2) == "httpGet" else "TCP socket"
        return MappingResult(
            f"the {transport} port of the {hook} lifecycle hook in {scope_label}",
            "container_rule",
            "high",
        )

    m = re.fullmatch(r"(livenessProbe|readinessProbe|startupProbe)\.(httpGet|tcpSocket)\.port", subpath)
    if m:
        probe = {
            "livenessProbe": "liveness probe",
            "readinessProbe": "readiness probe",
            "startupProbe": "startup probe",
        }[m.group(1)]
        transport = "HTTP GET" if m.group(2) == "httpGet" else "TCP socket"
        return MappingResult(
            f"the {transport} port of the {probe} in {scope_label}",
            "container_rule",
            "high",
        )

    if subpath == "ports[].protocol":
        return MappingResult(f"the protocol of a port exposed by {scope_label}", "container_rule", "high")
    if subpath == "resizePolicy[].resourceName":
        return MappingResult(f"the resource name in a resize policy of {scope_label}", "container_rule", "high")
    if subpath == "resources.limits":
        return MappingResult(f"the resource limits of {scope_label}", "container_rule", "high")
    if subpath == "resources.requests":
        return MappingResult(f"the resource requests of {scope_label}", "container_rule", "high")
    if subpath == "restartPolicyRules[].exitCodes.operator":
        return MappingResult(
            f"the exit-code operator in a restart policy rule of {scope_label}",
            "container_rule",
            "high",
        )
    if subpath == "securityContext.appArmorProfile.type":
        return MappingResult(f"the AppArmor profile type of {scope_label}", "container_rule", "high")
    if subpath == "securityContext.seccompProfile.type":
        return MappingResult(f"the seccomp profile type of {scope_label}", "container_rule", "high")
    if subpath == "volumeMounts[].bindMountOptions":
        return MappingResult(f"the bind-mount options of a volume mount in {scope_label}", "container_rule", "high")

    return None


def humanize_volume_subpath(subpath: str) -> MappingResult | None:
    # In-tree / named volume sources.
    first, dot, rest = subpath.partition(".")
    if first in VOLUME_SOURCE_LABELS and rest:
        source = VOLUME_SOURCE_LABELS[first]
        if rest == "fsType":
            return MappingResult(f"the filesystem type of {source}", "volume_rule", "high")
        if rest == "cachingMode":
            return MappingResult(f"the caching mode of {source}", "volume_rule", "high")
        if rest == "kind":
            return MappingResult(f"the kind of {source}", "volume_rule", "high")
        if rest == "storageMode":
            return MappingResult(f"the storage mode of {source}", "volume_rule", "high")

    if subpath == "downwardAPI.items[].resourceFieldRef.divisor":
        return MappingResult(
            "the resource field reference divisor in a Downward API volume item",
            "volume_rule",
            "high",
        )
    if subpath == "emptyDir.sizeLimit":
        return MappingResult("the size limit of an emptyDir volume", "volume_rule", "high")

    if subpath == "ephemeral.volumeClaimTemplate.spec.resources.limits":
        return MappingResult(
            "the storage resource limits of a generic ephemeral volume claim template",
            "volume_rule",
            "high",
        )
    if subpath == "ephemeral.volumeClaimTemplate.spec.resources.requests":
        return MappingResult(
            "the storage resource requests of a generic ephemeral volume claim template",
            "volume_rule",
            "high",
        )
    if subpath == "ephemeral.volumeClaimTemplate.spec.selector.matchExpressions[].operator":
        return MappingResult(
            "the operator of a selector match expression in a generic ephemeral volume claim template",
            "volume_rule",
            "high",
        )

    if subpath == "projected.sources[].clusterTrustBundle.labelSelector.matchExpressions[].operator":
        return MappingResult(
            "the operator of a label-selector match expression for a cluster trust bundle in a projected volume",
            "volume_rule",
            "high",
        )
    if subpath == "projected.sources[].downwardAPI.items[].resourceFieldRef.divisor":
        return MappingResult(
            "the resource field reference divisor in a Downward API source of a projected volume",
            "volume_rule",
            "high",
        )
    if subpath == "projected.sources[].podCertificate.keyType":
        return MappingResult(
            "the key type of a Pod certificate source in a projected volume",
            "volume_rule",
            "high",
        )

    return None


def humanize_pod_relative(relative: str) -> MappingResult | None:
    affinity = humanize_affinity(relative)
    if affinity:
        return affinity

    # Containers / initContainers / ephemeralContainers.
    m = re.match(r"^(containers|initContainers|ephemeralContainers)\[\]\.(.+)$", relative)
    if m:
        result = humanize_container_subpath(m.group(2), m.group(1))
        if result:
            return result

    if relative == "dnsPolicy":
        return MappingResult("its Pod DNS policy", "pod_rule", "high")
    if relative == "os.name":
        return MappingResult("its Pod operating system", "pod_rule", "high")
    if relative == "overhead":
        return MappingResult("its Pod overhead resource quantities", "pod_rule", "high")
    if relative == "preemptionPolicy":
        return MappingResult("its Pod preemption policy", "pod_rule", "high")
    if relative == "resources.limits":
        return MappingResult("its Pod-level resource limits", "pod_rule", "high")
    if relative == "resources.requests":
        return MappingResult("its Pod-level resource requests", "pod_rule", "high")
    if relative == "restartPolicy":
        return MappingResult("its Pod restart policy", "pod_rule", "high")
    if relative == "securityContext.appArmorProfile.type":
        return MappingResult("its Pod AppArmor profile type", "pod_rule", "high")
    if relative == "securityContext.fsGroupChangePolicy":
        return MappingResult("its Pod filesystem group change policy", "pod_rule", "high")
    if relative == "securityContext.seLinuxChangePolicy":
        return MappingResult("its Pod SELinux change policy", "pod_rule", "high")
    if relative == "securityContext.seccompProfile.type":
        return MappingResult("its Pod seccomp profile type", "pod_rule", "high")
    if relative == "securityContext.supplementalGroupsPolicy":
        return MappingResult("its Pod supplemental groups policy", "pod_rule", "high")
    if relative == "tolerations[].effect":
        return MappingResult("the effect of one of its Pod tolerations", "pod_rule", "high")
    if relative == "tolerations[].operator":
        return MappingResult("the operator of one of its Pod tolerations", "pod_rule", "high")
    if relative == "topologySpreadConstraints[].labelSelector.matchExpressions[].operator":
        return MappingResult(
            "the operator of a label-selector match expression in one of its topology spread constraints",
            "pod_rule",
            "high",
        )

    if relative.startswith("volumes[]."):
        result = humanize_volume_subpath(relative[len("volumes[]."):])
        if result:
            return result

    return None


def generic_humanize(path: str, kind: str) -> MappingResult:
    """Conservative fallback for resource types/paths without a semantic rule."""
    relative, is_pod_scope = strip_pod_prefix(path, kind)
    segments = path_segments(relative)

    # Find first array scope and render the remainder relative to one item.
    for idx, seg in enumerate(segments):
        if seg.endswith("[]"):
            collection = human_segment(seg)
            rest = segments[idx + 1 :]
            rest_text = " ".join(human_segment(s) for s in rest) or "configuration"
            phrase = f"the {rest_text} of one of its {collection}"
            return MappingResult(phrase, "generic_array_fallback", "low")

    # Drop structural scaffolding from the fallback wording.
    filtered = [s for s in segments if bare_segment(s) not in {"spec", "template", "metadata"}]
    text = " ".join(human_segment(s) for s in filtered) or "configuration"
    if is_pod_scope:
        return MappingResult(f"its Pod {text}", "generic_pod_fallback", "low")
    return MappingResult(f"its {text}", "generic_resource_fallback", "low")


def humanize_target(path: str, kind: str, overrides: dict[str, Any]) -> MappingResult:
    override = exact_override(path, overrides)
    if override:
        return override

    # Resource-level common patterns.
    if path == "spec.strategy.type":
        phrase = "its deployment strategy" if kind == "Deployment" else "its strategy type"
        return MappingResult(phrase, "resource_rule", "high")
    if path == "spec.strategy.rollingUpdate.maxSurge":
        return MappingResult("its maximum surge during rolling updates", "resource_rule", "high")
    if path == "spec.strategy.rollingUpdate.maxUnavailable":
        return MappingResult("its maximum unavailable replicas during rolling updates", "resource_rule", "high")
    if path == "spec.selector.matchExpressions[].operator":
        return MappingResult("the operator of a match expression in its selector", "resource_rule", "high")
    if path == "spec.type":
        label = "its Service type" if kind == "Service" else f"its {kind} type"
        return MappingResult(label, "resource_rule", "high")
    if path == "spec.externalTrafficPolicy":
        return MappingResult("its external traffic policy", "resource_rule", "high")
    if path == "spec.internalTrafficPolicy":
        return MappingResult("its internal traffic policy", "resource_rule", "high")
    if path == "spec.sessionAffinity":
        return MappingResult("its session affinity", "resource_rule", "high")
    if path == "spec.ports[].protocol":
        return MappingResult("the protocol of one of its ports", "resource_rule", "high")
    if path == "spec.ports[].targetPort":
        return MappingResult("the target port of one of its ports", "resource_rule", "high")
    if path == "spec.ports[].port":
        return MappingResult("the port number of one of its ports", "resource_rule", "high")

    # EDGE points still receive deterministic, human-readable prompts. These
    # rules affect wording only; classify_case() keeps them out of PRIMARY.
    if path == "metadata.managedFields[].operation":
        return MappingResult(
            "the operation of one of its managed fields",
            "edge_rule",
            "high",
        )
    if path.endswith(".template.metadata.managedFields[].operation"):
        return MappingResult(
            "the operation of one of its Pod template managed fields",
            "edge_rule",
            "high",
        )
    if path.endswith(".ephemeral.volumeClaimTemplate.metadata.managedFields[].operation"):
        return MappingResult(
            "the operation of one of the managed fields in a generic ephemeral volume claim template",
            "edge_rule",
            "high",
        )
    if path == "status.conditions[].status":
        return MappingResult(
            "the status value of one of its status conditions",
            "edge_rule",
            "high",
        )

    relative, is_pod_scope = strip_pod_prefix(path, kind)
    if is_pod_scope:
        result = humanize_pod_relative(relative)
        if result:
            return result

    return generic_humanize(path, kind)


def variant_labels(point_feature: str, variants: Iterable[str]) -> list[str]:
    prefix = point_feature + "_"
    labels: list[str] = []
    for variant in variants:
        variant = str(variant)
        labels.append(variant[len(prefix):] if variant.startswith(prefix) else variant)
    return labels


def build_user_prompt(kubernetes_version: str, kind: str, target_phrase: str) -> str:
    return (
        f"Kubernetes version: {kubernetes_version}\n\n"
        f"Create a Kubernetes {kind} that explicitly configures {target_phrase}."
    )


def safe_filename(case_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", case_id).strip("_") or "case"


def process_profile(
    profile: dict[str, Any],
    kubernetes_version: str,
    overrides: dict[str, Any],
) -> dict[str, Any]:
    gvk = profile.get("gvk") or {}
    kind = gvk.get("kind")
    api_version = gvk.get("apiVersion")
    if not kind:
        raise ValueError("Input profile is missing gvk.kind")
    if not api_version:
        raise ValueError("Input profile is missing gvk.apiVersion")

    points = profile.get("points")
    if not isinstance(points, list):
        raise ValueError("Input profile is missing a 'points' array")

    cases: list[dict[str, Any]] = []
    counts = {"READY": 0, "REVIEW": 0, "SKIP": 0}
    group_counts: dict[str, int] = {}
    classification_counts: dict[str, int] = {}
    mapping_counts: dict[str, int] = {}
    plan_counts: dict[str, int] = {}

    for point in points:
        config_path = str(point.get("config_path", "")).strip()
        point_feature = str(point.get("point_feature", "")).strip()
        if not config_path or not point_feature:
            continue

        classification = classify_case(point)
        mapping = humanize_target(config_path, kind, overrides)

        prompt_status = classification.prompt_status
        prompt_reason = classification.reason
        if prompt_status == "READY" and mapping.confidence == "low":
            prompt_status = "REVIEW"
            prompt_reason = (
                f"{prompt_reason};generic_humanization_requires_review"
                if prompt_reason
                else "generic_humanization_requires_review"
            )

        counts[prompt_status] = counts.get(prompt_status, 0) + 1
        group_counts[classification.benchmark_group] = group_counts.get(classification.benchmark_group, 0) + 1
        classification_counts[classification.classification] = (
            classification_counts.get(classification.classification, 0) + 1
        )
        mapping_counts[mapping.method] = mapping_counts.get(mapping.method, 0) + 1
        plan = str(point.get("plan_type", ""))
        plan_counts[plan] = plan_counts.get(plan, 0) + 1

        case = {
            "case_id": point.get("case_id"),
            "resource": kind,
            "api_version": api_version,
            "kubernetes_version": kubernetes_version,
            "point_feature": point_feature,
            "config_path": config_path,
            "relation_type": point.get("relation_type"),
            "relation_index": point.get("relation_index"),
            "variant_features": list(point.get("variants") or []),
            "variant_labels": variant_labels(point_feature, point.get("variants") or []),
            "plan_type": point.get("plan_type"),
            "n_array_ancestors": point.get("n_array_ancestors", 0),
            "source_status": point.get("status"),
            "target_phrase": mapping.phrase,
            "mapping_method": mapping.method,
            "mapping_confidence": mapping.confidence,
            "prompt_status": prompt_status,
            "prompt_status_reason": prompt_reason,
            "benchmark_group": classification.benchmark_group,
            "classification": classification.classification,
            "user_prompt": build_user_prompt(kubernetes_version, kind, mapping.phrase),
        }
        cases.append(case)

    return {
        "format": FORMAT_VERSION,
        "source_profile_format": profile.get("format"),
        "root_feature": profile.get("root_feature"),
        "gvk": {"apiVersion": api_version, "kind": kind},
        "kubernetes_version": kubernetes_version,
        "semantics": (
            "Each prompt requests only the variation point. Alternative values are retained as benchmark metadata "
            "but are not exposed to the LLM. Prompt readiness is independent from benchmark grouping: PRIMARY "
            "contains ordinary user-configurable points, while EDGE retains semantically special points for a "
            "separate FM-vs-LLM analysis instead of discarding them."
        ),
        "summary": {
            "cases_total": len(cases),
            "by_prompt_status": counts,
            "by_benchmark_group": dict(sorted(group_counts.items())),
            "by_classification": dict(sorted(classification_counts.items())),
            "by_mapping_method": dict(sorted(mapping_counts.items())),
            "by_plan_type": dict(sorted(plan_counts.items())),
        },
        "cases": cases,
    }


def write_csv(dataset: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "case_id",
        "resource",
        "api_version",
        "kubernetes_version",
        "config_path",
        "point_feature",
        "variant_labels",
        "plan_type",
        "n_array_ancestors",
        "target_phrase",
        "mapping_method",
        "mapping_confidence",
        "prompt_status",
        "prompt_status_reason",
        "benchmark_group",
        "classification",
        "user_prompt",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for case in dataset["cases"]:
            row = {k: case.get(k, "") for k in fields}
            row["variant_labels"] = " | ".join(case.get("variant_labels") or [])
            writer.writerow(row)


def write_prompt_files(dataset: dict[str, Any], directory: Path, include_review: bool = False) -> int:
    """Write prompt files grouped by benchmark group.

    Output layout:
      <directory>/primary/*.txt
      <directory>/edge/*.txt

    REVIEW cases are only emitted when include_review=True. SKIP cases are never
    emitted, although they remain in the JSON/CSV dataset for traceability.
    """
    directory.mkdir(parents=True, exist_ok=True)
    allowed = {"READY"}
    if include_review:
        allowed.add("REVIEW")

    written = 0
    for case in dataset["cases"]:
        if case["prompt_status"] not in allowed:
            continue

        group = str(case.get("benchmark_group", "UNASSIGNED")).strip().lower() or "unassigned"
        target_dir = directory / group
        target_dir.mkdir(parents=True, exist_ok=True)

        filename = safe_filename(str(case["case_id"])) + ".txt"
        (target_dir / filename).write_text(case["user_prompt"] + "\n", encoding="utf-8")
        written += 1
    return written


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="auto_profile_*_all_points.json")
    parser.add_argument("--kubernetes-version", required=True, help="Cluster version, e.g. 1.37")
    parser.add_argument("--output", type=Path, default=Path("llm_cases.json"), help="Output JSON dataset")
    parser.add_argument("--csv", dest="csv_path", type=Path, help="Optional CSV review table")
    parser.add_argument(
        "--prompts-dir",
        type=Path,
        help="Optional directory for prompt files, grouped into primary/ and edge/",
    )
    parser.add_argument("--include-review-prompts", action="store_true", help="Also emit REVIEW prompt files")
    parser.add_argument("--overrides", type=Path, help="Optional JSON with exact config_path -> phrase overrides")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    profile = json.loads(args.input.read_text(encoding="utf-8"))
    overrides = load_overrides(args.overrides)
    dataset = process_profile(profile, args.kubernetes_version, overrides)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(dataset, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if args.csv_path:
        write_csv(dataset, args.csv_path)

    prompts_written = 0
    if args.prompts_dir:
        prompts_written = write_prompt_files(dataset, args.prompts_dir, args.include_review_prompts)

    summary = dataset["summary"]
    print(f"Resource: {dataset['gvk']['kind']} ({dataset['gvk']['apiVersion']})")
    print(f"Cases: {summary['cases_total']}")
    print("Prompt status:", summary["by_prompt_status"])
    print("Benchmark groups:", summary["by_benchmark_group"])
    print("Classifications:", summary["by_classification"])
    print("Mapping methods:", summary["by_mapping_method"])
    print(f"JSON: {args.output}")
    if args.csv_path:
        print(f"CSV: {args.csv_path}")
    if args.prompts_dir:
        print(f"Prompt files: {prompts_written} -> {args.prompts_dir}")


if __name__ == "__main__":
    main()