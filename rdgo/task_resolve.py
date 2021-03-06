# Copyright (C) 2015 Colin Walters <walters@verbum.org>
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the
# Free Software Foundation, Inc., 59 Temple Place - Suite 330,
# Boston, MA 02111-1307, USA.

import os
import json
import argparse
import subprocess
import errno
import shutil
import tempfile

from .utils import log, fatal, ensuredir, rmrf, ensure_clean_dir, run_sync
from .basetask_resolve import BaseTaskResolve
from . import specfile 
from .git import GitRemote

def require_key(conf, key):
    try:
        return conf[key]
    except KeyError:
        fatal("Missing config key {0}".format(key))

class TaskResolve(BaseTaskResolve):
    def __init__(self):
        BaseTaskResolve.__init__(self)
        self._srpm_mock_initialized = None

    def _json_dumper(self, obj):
        if isinstance(obj, GitRemote):
            return obj.url
        else:
            return obj

    def _tar_czf_with_prefix(self, dirpath, prefix, output):
        dn = os.path.dirname(dirpath)
        bn = os.path.basename(dirpath)
        run_sync(['tar', '--exclude-vcs', '-czf', output, '--transform', 's,^' + bn + ',' + prefix + ',', bn],
                 cwd=dn)

    def _strip_all_prefixes(self, s, prefixes):
        for prefix in prefixes:
            if s.startswith(prefix):
                s = s[len(prefix):]
        return s

    def _rpm_verrel(self, component, upstream_tag, upstream_rev, distgit_desc):
        override_version = component.get('override-version')
        gitdesc = upstream_rev or ''
        if distgit_desc is not None:
            if gitdesc != '':
                gitdesc += '.'
            gitdesc += distgit_desc.replace('-', '.')
        if override_version is not None:
            return [override_version, gitdesc]
        rpm_version = upstream_tag or '0'
        # Some common patterns out there.
        known_tag_prefixes = ['v']
        for prefix in [component['pkgname'],component['pkgname'].upper()]:
            for suffix in ['-', '_']:
                known_tag_prefixes.append(prefix + suffix)
        rpm_version = self._strip_all_prefixes(rpm_version, known_tag_prefixes)
        rpm_version = rpm_version.replace('-', '.')
        return [rpm_version, gitdesc]

    def _generate_srcsnap_impl(self, component, upstream_tag, upstream_rev, upstream_co,
                               distgit_desc, distgit_co, target):
        distgit = component.get('distgit')
        if distgit is not None:
            patches_action = distgit.get('patches', None)
        else:
            patches_action = None

        upstream_desc = upstream_rev
        if upstream_tag is not None:
            upstream_desc = upstream_tag + '-' + upstream_desc

        [rpm_version, rpm_release] = self._rpm_verrel(component, upstream_tag, upstream_rev, distgit_desc)

        spec_fn = specfile.spec_fn(spec_dir=distgit_co)
        spec = specfile.Spec(distgit_co + '/' + spec_fn)

        if upstream_desc is not None:
            tar_dirname = '{0}-{1}'.format(component['name'], upstream_desc)
            tarname = tar_dirname + '.tar.gz'
            tmp_tarpath = distgit_co + '/' + tarname
            self._tar_czf_with_prefix(upstream_co, tar_dirname, tmp_tarpath)
            rmrf(upstream_co)
            has_zero = spec.get_tag('Source0', allow_empty=True) is not None
            source_tag = 'Source'
            if has_zero:
                source_tag += '0'
            spec.set_tag(source_tag, tarname)
            # This is a currently ad-hoc convention
            spec.set_global('commit', upstream_rev)
            spec.set_tag('Version', rpm_version)
            spec.set_setup_dirname(tar_dirname)
            spec.set_tag('Release', rpm_release + '%{?dist}')

        # Anything useful there you should find in upstream dist-git or equivalent.
        spec.delete_changelog()
        # Forcibly override
        # spec.set_tag('Epoch', '99')
        if patches_action in (None, 'keep'):
            pass
        elif patches_action == 'drop':
            spec.wipe_patches()
        else:
            fatal("Component '{0}': Unknown patches action '{1}'".format(component['name'],
                                                                         patches_action))
        spec.save()
        spec._txt = '# NOTE: AUTO-GENERATED by rpmdistro-gitoverlay; DO NOT EDIT\n' + spec._txt

        sources_path = distgit_co + '/sources'
        if os.path.exists(sources_path):
            # Exec as an external binary because pyrpkg is python 2 only, and
            # mock is Python 3 only.  Sigh.
            subprocess.check_call([PKGLIBDIR + '/rpkg-prep-sources',  # noqa pylint: disable=undefined-variable
                                   '--distgit-name='+distgit['name'],
                                   '--distgit-url='+distgit['src'].url,
                                   '--distgit-co='+distgit_co,
                                   '--lookaside-mirror='+self.lookaside_mirror])
                     
        shutil.move(distgit_co, self.tmp_snapshotdir + '/' + target)

    def _generate_srcsnap(self, component):
        upstream_src = component.get('src')
        if upstream_src is not None:
            upstream_rev = component['revision']
            [upstream_tag, upstream_rev] = self.mirror.describe(upstream_src, upstream_rev)
            upstream_desc = upstream_rev
            if upstream_tag is not None:
                upstream_desc = upstream_tag + '-' + upstream_desc
        else:
            upstream_rev = upstream_tag = upstream_desc = None

        distgit = component.get('distgit')
        if distgit is not None:
            distgit_src = distgit['src']
            distgit_rev = distgit['revision']
            [distgit_tag, distgit_rev] = self.mirror.describe(distgit_src, distgit_rev)
            distgit_desc = distgit_rev
            if distgit_tag is not None:
                distgit_desc = distgit_tag + '-' + distgit_desc
        else:
            distgit_desc = None

        assert (upstream_desc or distgit_desc) is not None

        [rpm_version, rpm_release] = self._rpm_verrel(component, upstream_tag, upstream_rev, distgit_desc)

        srcsnap_name = "{0}-{1}-{2}.srcsnap".format(component['pkgname'], rpm_version, rpm_release)
        tmpdir = tempfile.mkdtemp('', 'rdgo-srpms', self.tmpdir)
        try:
            if upstream_src is not None:
                upstream_co = tmpdir + '/' + component['name']
                self.mirror.checkout(upstream_src, upstream_rev, upstream_co)
            else:
                upstream_co = None

            if distgit is not None:
                distgit_topdir = tmpdir + '/' + 'distgit'
                ensure_clean_dir(distgit_topdir)
                # Create a directory whose name matches the module
                # name, which helps fedpkg/rhpkg.
                distgit_co = distgit_topdir + '/' + distgit['name']
                self.mirror.checkout(distgit_src, distgit_rev, distgit_co)
            else:
                specfn = self._find_spec(upstream_co)
                if specfn is None:
                    fatal("Failed to find .spec (or .spec.in) file")
                if specfn.endswith('.in'):
                    dest_specfn = tmpdir + '/' + os.path.basename(specfn[:-3])
                else:
                    dest_specfn = tmpdir
                shutil.copy2(specfn, dest_specfn)
                distgit_co = tmpdir

            self._generate_srcsnap_impl(component, upstream_tag, upstream_rev, upstream_co,
                                        distgit_desc, distgit_co,
                                        srcsnap_name)
        finally:
            if 'PRESERVE_TEMP' not in os.environ:
                rmrf(tmpdir)
        return srcsnap_name

    def run(self, argv):
        parser = argparse.ArgumentParser(description="Create snapshot.json")
        parser.add_argument('--tempdir', action='store', default=None,
                            help='Path to directory for temporary working files')
        parser.add_argument('--fetch-all', action='store_true', help='Fetch all git repositories')
        parser.add_argument('-f', '--fetch', action='append', default=[],
                            help='Fetch the specified git repository')
        parser.add_argument('--override-giturl', action='store',
                            help='If the provided git URL if it is in the overlay, prepare to override it, otherwise exit 77')
        parser.add_argument('--override-gitbranch', action='store',
                            help='Use with --override-giturl to specify a branch')
        parser.add_argument('--override-gitrepo-from', action='store',
                            help='Pull from this local git repository')
        parser.add_argument('--override-gitrepo-from-rev', action='store',
                            help='Use with --override-gitrepo-from to specify an expected revision')
        parser.add_argument('--touch-if-changed', action='store', default=None,
                            help='Create or update timestamp on target path if a change occurred')
        parser.add_argument('-b', '--build', action='store_true', 
                            help='If fetch changes, automatically do a build')

        opts = parser.parse_args(argv)

        srcdir = self.workdir + '/src'
        if not os.path.isdir(srcdir):
            fatal("Missing src/ directory; run 'rpmdistro-gitoverlay init'?")
        if os.path.islink(srcdir):
            fatal("src/ directory is a symbolic link; is this a thin clone?")

        self._load_overlay()

        self.tmpdir = opts.tempdir
        self.old_snapshotdir = self.workdir + '/old-snapshot'
        self.snapshotdir = self.workdir + '/snapshot'
        self.tmp_snapshotdir = self.snapshotdir + '.tmp'
        ensure_clean_dir(self.tmp_snapshotdir)

        ensuredir(self.lookaside_mirror)

        expanded = self._expand_overlay(fetchall=opts.fetch_all, fetch=opts.fetch,
                                        override_giturl=opts.override_giturl,
                                        override_gitbranch=opts.override_gitbranch,
                                        override_gitrepo_from=opts.override_gitrepo_from,
                                        override_gitrepo_from_rev=opts.override_gitrepo_from_rev)

        for component in expanded['components']:
            srcsnap = self._generate_srcsnap(component)
            component['srcsnap'] = os.path.basename(srcsnap)

        snapshot_path = self.snapshotdir + '/snapshot.json'
        snapshot_tmppath = self.tmp_snapshotdir + '/snapshot.json'
        with open(snapshot_tmppath, 'w') as f:
            json.dump(expanded, f, indent=4, sort_keys=True, default=self._json_dumper)

        rmrf(self.old_snapshotdir)

        changed = True
        if (os.path.exists(snapshot_path) and subprocess.call(['cmp', '-s', snapshot_path, snapshot_tmppath]) == 0):
            changed = False
        if changed:
            try:
                os.rename(self.snapshotdir, self.old_snapshotdir)
            except OSError as e:
                if e.errno != errno.ENOENT:
                    raise
            os.rename(self.tmp_snapshotdir, self.snapshotdir)
            log("Wrote: " + self.snapshotdir)
            if opts.touch_if_changed:
                with open(opts.touch_if_changed, 'a'):
                    log("Updated timestamp of {}".format(opts.touch_if_changed))
                    os.utime(opts.touch_if_changed, None)
            if opts.build:
                os.execlp('rpmdistro-gitoverlay', 'rpmdistro-gitoverlay', 'build')
        else:
            rmrf(self.tmp_snapshotdir)
            log("No changes.")
