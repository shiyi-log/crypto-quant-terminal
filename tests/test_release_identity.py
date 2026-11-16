"""发布版本与标签身份，不依赖 GitHub 或真实凭据。"""
import pytest

from tools.release_identity import release_tag_for_version


def test_untagged_commit_is_only_a_branch_package():
    assert release_tag_for_version('0.2.1', []) == ''


def test_exactly_one_matching_version_tag_can_publish():
    assert release_tag_for_version('0.2.1', ['v0.2.1']) == 'v0.2.1'


@pytest.mark.parametrize('version,tags', [
    ('0.2.1', ['v0.2.0']), ('0.2.1', ['v0.2.0', 'v0.2.1']),
    ('0.2.1', ['v0.2.1-rc.1']), ('0.2.1', ['v0.2.1', 'v0.2.1']),
    ('0.2', []), ('0.2.1\nunsafe', []), ('0.2.1', ['unrelated-tag']),
])
def test_ambiguous_mismatched_or_malformed_identity_is_rejected(version, tags):
    with pytest.raises(ValueError):
        release_tag_for_version(version, tags)
