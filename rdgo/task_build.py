#!/usr/bin/env python
#
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
import StringIO
import subprocess
import hashlib
import yaml
import tempfile
import copy

from .swappeddir import SwappedDirectory
from .utils import log, fatal, ensuredir, rmrf, ensure_clean_dir, run_sync
from .task import Task
from . import specfile 
from .git import GitMirror
from .mockchain import main as mockchain_main

def require_key(conf, key):
    try:
        return conf[key]
    except KeyError, e:
        fatal("Missing config key {0}".format(key))

class TaskBuild(Task):

    def _tar_czf_with_prefix(self, dirpath, prefix, output):
        dn = os.path.dirname(dirpath)
        bn = os.path.basename(dirpath)
        run_sync(['tar', '--exclude-vcs', '-czf', output, '--transform', 's,^' + bn + ',' + prefix + ',', bn],
                 cwd=dn)

    def _rpm_verrel(self, component, upstream_tag, upstream_rev, distgit_desc):
        rpm_version = upstream_tag or '0'
        if rpm_version.startswith('v'):
            rpm_version = rpm_version[1:]
        rpm_version = rpm_version.replace('-', '.')
        return [rpm_version, upstream_rev + '.' + distgit_desc.replace('-', '.')]

    def _generate_srpm(self, component, upstream_tag, upstream_rev, upstream_co, distgit_desc, distgit_co, target):
        distgit = component['distgit']
        patches_action = distgit.get('patches', None)

        upstream_desc = upstream_rev
        if upstream_tag is not None:
            upstream_desc = upstream_tag + '-' + upstream_desc

        [rpm_version, rpm_release] = self._rpm_verrel(component, upstream_tag, upstream_rev, distgit_desc)

        tar_dirname = '{0}-{1}'.format(component['name'], upstream_desc)
        tarname = tar_dirname + '.tar.gz'
        tmp_tarpath = distgit_co + '/' + tarname
        self._tar_czf_with_prefix(upstream_co, tar_dirname, tmp_tarpath)
        spec_fn = specfile.spec_fn(spec_dir=distgit_co)
        spec = specfile.Spec(distgit_co + '/' + spec_fn)
        spec.set_tag('Source0', tarname)
        spec.set_tag('Version', rpm_version)
        spec.set_tag('Release', rpm_release + '%{?dist}')
        spec.set_setup_dirname(tar_dirname)
        # Forcibly override
        spec.set_tag('Epoch', '99')
        if patches_action in (None, 'keep'):
            pass
        elif patches_action == 'drop':
            spec.wipe_patches()
        else:
            fatal("Component '{0}': Unknown patches action '{1}'".format(component['name'],
                                                                         patches_action))
        spec.save()
        spec._txt = '# NOTE: AUTO-GENERATED by rpmdistro-gitoverlay; DO NOT EDIT\n' + spec._txt
        rpmbuild_argv = ['rpmbuild']
        for v in ['_sourcedir', '_specdir', '_builddir',
                  '_srcrpmdir', '_rpmdir']:
            rpmbuild_argv.extend(['--define', '%' + v + ' ' + distgit_co])
        rpmbuild_argv.extend(['-bs', spec_fn])
        run_sync(rpmbuild_argv, cwd=distgit_co)
        srpms = []
        for fname in os.listdir(distgit_co):
            if fname.endswith('.src.rpm'):
                srpms.append(fname)
        if len(srpms) == 0:
            fatal("No .src.rpm found in {0}".format(distgit_co))
        elif len(srpms) > 1:
            fatal("Multiple .src.rpm found in {0}".format(distgit_co))
        srpm = srpms[0]
        os.link(distgit_co + '/' + srpm, self.srpmdir + '/' + target)

    def _ensure_srpm(self, component):
        upstream_src = component['src']
        upstream_rev = component['revision']
        [upstream_tag, upstream_rev] = self.mirror.describe(upstream_src, upstream_rev)
        upstream_desc = upstream_rev
        if upstream_tag is not None:
            upstream_desc = upstream_tag + '-' + upstream_desc
        distgit = component['distgit']
        distgit_src = distgit['src']
        distgit_rev = distgit['revision']
        [distgit_tag, distgit_rev] = self.mirror.describe(distgit_src, distgit_rev)
        distgit_desc = distgit_rev
        if distgit_tag is not None:
            distgit_desc = distgit_tag + '-' + distgit_desc

        [rpm_version, rpm_release] = self._rpm_verrel(component, upstream_tag, upstream_rev, distgit_desc)

        name = "{0}-{1}-{2}.src.rpm".format(distgit['name'],
                                            rpm_version, rpm_release)
        tmpdir = tempfile.mkdtemp('', 'rdgo-srpms')
        try:
            upstream_co = tmpdir + '/' + component['name']
            self.mirror.checkout(upstream_src, upstream_rev, upstream_co)
            distgit_co = tmpdir + '/' + 'distgit-' + distgit['name']
            self.mirror.checkout(distgit_src, distgit_rev, distgit_co)

            self._generate_srpm(component, upstream_tag, upstream_rev, upstream_co, distgit_desc, distgit_co, name)
        finally:
            if not 'PRESERVE_TEMP' in os.environ:
                rmrf(tmpdir)
        return name

    def _assert_get_one_child(self, path):
        results = os.listdir(path)
        if len(results) == 0:
            fatal("No files found in {0}".format(path))
        if len(results) > 1:
            fatal("Too many files found in {0}: {1}".format(path, results))
        return path + '/' + results[0]

    def _json_hash(self, dictval):
        """Kind of a hack, but it works."""
        serialized = json.dumps(dictval, sort_keys=True)
        h = hashlib.sha256()
        h.update(serialized)
        return h.hexdigest()

    def run(self):
        snapshot = self.get_snapshot()

        root = require_key(snapshot, 'root')
        root_mock = require_key(root, 'mock')

        self.mirror = GitMirror(self.workdir + '/src')
        self.rpmdir = SwappedDirectory(self.workdir + '/rpms')

        self.newrpms = self.rpmdir.prepare()

        self.srpmdir = self.newrpms + '/srpms'
        ensuredir(self.srpmdir)

        mc_argv = ['mockchain', '--recurse', '-r', root_mock,
                   '-l', self.newrpms]

        oldcache_path = self.rpmdir.path + '/buildstate.json'
        oldcache = {}
        if os.path.exists(oldcache_path):
            with open(oldcache_path) as f:
                oldcache = json.load(f)
        newcache = {}
        newcache_path = self.newrpms + '/buildstate.json'

        need_build = False
        for component in snapshot['components']:
            component_hash = self._json_hash(component)
            distgit_name = component['distgit']['name']
            cachedstate = oldcache.get(distgit_name)
            if cachedstate is not None:
                if cachedstate['hashv0'] == component_hash:
                    cached_dirname = cachedstate['dirname']
                    log("Reusing cached build: {0}".format(cached_dirname))
                    oldrpmdir = self.rpmdir.path + '/' + cached_dirname
                    newrpmdir = self.newrpms + '/' + cached_dirname
                    subprocess.check_call(['cp', '-al', oldrpmdir, newrpmdir])
                    continue
            srpm = self._ensure_srpm(component)
            assert srpm.endswith('.src.rpm')
            srpm_version = srpm[:-len('.src.rpm')]
            newcache[distgit_name] = {'hashv0': component_hash,
                                      'dirname': srpm_version}
            mc_argv.append(self.srpmdir + '/' + srpm)
            need_build = True
            with open(newcache_path, 'w') as f:
                json.dump(newcache, f, sort_keys=True)

        if need_build:
            log("Performing mockchain: {0}".format(mc_argv))
            rc = mockchain_main(mc_argv) 
            if rc != 0:
                fatal("mockchain exited with code {0}".format(rc))
        else:
            ensuredir(self.newrpms + '/repodata')
            run_sync(['createrepo_c', 'repodata'], cwd=self.newrpms)            

        self.rpmdir.commit()

        log("Success!")

