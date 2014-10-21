# Copyright 2012 litl, LLC.  Licensed under the MIT license.

import logging

from flask import Blueprint, current_app, json, request, Response, abort
from werkzeug.exceptions import BadRequest, NotFound

from . import github, jenkins

#from datetime import datetime

base = Blueprint("base", __name__)


@base.route("/ping")
def ping():
    return "pong"


def _parse_jenkins_json(request):
    # The Jenkins notification plugin (at least as of 1.4) incorrectly sets
    # its Content-type as application/x-www-form-urlencoded instead of
    # application/json.  As a result, all of the data gets stored as a key
    # in request.form.  Try to detect that and deal with it.
    if len(request.form) == 1:
        try:
            return json.loads(request.form.keys()[0])
        except ValueError:
            # Seems bad that there's only 1 key, but press on
            return request.form
    else:
        return request.json


@base.route("/notification/jenkins", methods=["POST"])
def jenkins_notification():
    data = _parse_jenkins_json(request)

    jenkins_name = data["name"]
    jenkins_number = data["build"]["number"]
    jenkins_url = data["build"]["full_url"]
    phase = data["build"]["phase"]

    logging.debug("Received Jenkins notification for %s %s (%s): %s",
                  jenkins_name, jenkins_number, jenkins_url, phase)

    if phase not in ("STARTED", "COMPLETED"):
        return Response(status=204)

    git_base_repo = data["build"]["parameters"]["GIT_BASE_REPO"]
    git_sha1 = data["build"]["parameters"]["GIT_SHA1"]

    repo_config = github.get_repo_config(current_app, git_base_repo)

    if repo_config is None:
        err_msg = "No repo config for {0}".format(git_base_repo)
        logging.warn(err_msg)
        raise NotFound(err_msg)

    #desc_prefix = "Jenkins build '{0}' #{1}".format(jenkins_name,
    #                                                jenkins_number)
    desc_prefix = "Build #{0}".format(jenkins_number)

    # replace branch name with sha and check for auto-merge commit
    commit = github.get_commit(current_app, repo_config,
                               git_base_repo, git_sha1)
    if git_sha1 != commit["sha"]:
        git_sha1 = commit["sha"]
    parents = [p["sha"] for p in commit["parents"]]
    message = "Merge {1} into {0}"
    if len(parents) > 1 and \
       commit["commit"]["message"] == message.format(parents[0], parents[1]):
    #if commit["message"].startswith("Merge"): # TODO: more robust method
        #git_sha1 = commit["parents"][1]["sha"] # second commit ref
        git_sha1 = parents[1] # second commit ref
        logging.debug("Parent of auto-merge commit found: " + git_sha1)

    if phase == "STARTED":
        github_state = "pending"
        github_desc = desc_prefix + " has started"
    else:
        status = data["build"]["status"]

        # build duration
        build_info = jenkins.get_build(current_app,
                          repo_config,
                          jenkins_number)
        #logging.debug(build_info)
        duration = round(int(build_info["duration"]) * 0.001)
        #curr_status = github.get_status(current_app, repo_config, git_base_repo, git_sha1).json
	#start_time = curr_status[0]["created_at"]
        #start_time = datetime.strptime(start_time, "%Y-%m-%dT%H:%M:%SZ")
        #duration = (datetime.now() - start_time).seconds % (60*60)

        if status == "SUCCESS":
            github_state = "success"
            #github_desc = desc_prefix + " has succeeded"
            github_desc = desc_prefix + " succeeded in {0:.0f}s".format(duration)
        elif status == "FAILURE" or status == "UNSTABLE":
            github_state = "failure"
            github_desc = desc_prefix + " failed in {0:.0f}s".format(duration)
        elif status == "ABORTED":
            github_state = "error"
            github_desc = desc_prefix + " has encountered an error"
        else:
            logging.debug("Did not understand '%s' build status. Aborting.",
                          status)
            abort()

    logging.debug(github_desc)

    github.update_status(current_app,
                         repo_config,
                         git_base_repo,
                         git_sha1,
                         github_state,
                         github_desc,
                         jenkins_url)

    return Response(status=204)


@base.route("/notification/github", methods=["POST"])
def github_notification():
    event_type = request.headers.get("X-GitHub-Event")
    if event_type is None:
        msg = "Got GitHub notification without a type"
        logging.warn(msg)
        return BadRequest(msg)
    elif event_type == "ping":
        return Response(status=200)
    elif event_type != "pull_request":
        msg = "Got unknown GitHub notification event type: %s" % (event_type,)
        logging.warn(msg)
        return BadRequest(msg)

    action = request.json["action"]
    pull_request = request.json["pull_request"]
    number = pull_request["number"]
    html_url = pull_request["html_url"]
    base_repo_name = github.get_repo_name(pull_request, "base")

    logging.debug("Received GitHub pull request notification for "
                  "%s %s (%s): %s",
                  base_repo_name, number, html_url, action)

    if action not in ("opened", "reopened", "synchronize"):
        logging.debug("Ignored '%s' action." % action)
        return Response(status=204)

    repo_config = github.get_repo_config(current_app, base_repo_name)

    if repo_config is None:
        err_msg = "No repo config for {0}".format(base_repo_name)
        logging.warn(err_msg)
        raise NotFound(err_msg)

    head_repo_name, shas = github.get_commits(current_app,
                                              repo_config,
                                              pull_request)

    logging.debug("Trigging builds for %d commits", len(shas))

    html_url = pull_request["html_url"]

    for sha in shas:
        github.update_status(current_app,
                             repo_config,
                             base_repo_name,
                             sha,
                             "pending",
                             "Jenkins build is being scheduled")

        logging.debug("Scheduling build for %s %s", head_repo_name, sha)
        jenkins.schedule_build(current_app,
                               repo_config,
                               head_repo_name,
                               sha,
                               html_url)

    return Response(status=204)
