#!/usr/bin/python3
# coding=utf-8

#   Copyright 2026 EPAM Systems
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

""" Version labels for usage analytics (#7033) """

import re

from . import _analytics as an
from . import _skill_analytics as sa

VERSION_SUFFIX = re.compile(an.VERSION_SUFFIX_PATTERN)


def label_versions(project_id, versions):
    versions = [version for version in versions if version["version_id"] is not None]
    skill_versions = [version for version in versions if version["root_entity_type"] == an.ENTITY_TYPE_SKILL]
    other_versions = [version for version in versions if version["root_entity_type"] != an.ENTITY_TYPE_SKILL]
    return {**label_agent_versions(project_id, other_versions), **label_skill_versions(project_id, skill_versions)}


def build_version_key(project_id, version):
    return version["root_project_id"] or project_id, version["version_id"]


def is_local_agent_version(project_id, version):
    return version["root_entity_type"] in an.AGENT_ROOT_TYPES and version["root_project_id"] in (None, project_id)


def label_agent_versions(project_id, versions):
    local_versions = [version for version in versions if is_local_agent_version(project_id, version)]
    _, core_versions = an.entity_meta(project_id, version_ids=[version["version_id"] for version in local_versions])
    labels = {}
    for version in versions:
        core_version = core_versions.get(version["version_id"]) or {}
        is_own_version = (
            is_local_agent_version(project_id, version)
            and core_version.get("application_id") == version["root_entity_id"]
        )
        stored_suffix = VERSION_SUFFIX.search(version["stored_name"] or "")
        labels[build_version_key(project_id, version)] = (
            (is_own_version and core_version.get("name"))
            or (stored_suffix and stored_suffix.group(1))
            or f"Version #{version['version_id']}"
        )
    return labels


def label_skill_versions(project_id, versions):
    refs = {(version["root_project_id"] or project_id, version["root_entity_id"]) for version in versions}
    skills, _ = sa.fetch_skill_meta(project_id, refs)
    labels = {}
    for version in versions:
        skill = (skills or {}).get((version["root_project_id"] or project_id, version["root_entity_id"])) or {}
        names = {item.get("id"): item.get("name") for item in skill.get("versions") or []}
        labels[build_version_key(project_id, version)] = names.get(version["version_id"]) or f"Version #{version['version_id']}"
    return labels
