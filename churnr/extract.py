#!/usr/bin/env python
# -*- coding: utf-8 -*-
import os
import logging
import uuid
import argparse
from google.cloud import bigquery as bq
import datetime as dt
import math
import json
import shutil

import churnr.utils as utils

SECS_IN_DAY = 86399

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('churnr.extract')


def main(exppath, experiment, dsname, hddump):
    """ Extract data from several sources on BigQuery using intermediary tables
        and dump the final output as a file into ../data/raw
    """
    logger.info('Starting BigQuery data extraction...')

    with open(exppath) as f:
        exp = json.load(f)

    # load experiment configuration
    keys = ['project', 'dataset', 'enddate', 'obsdays', 'preddays', 'shareusers', 'timesplits', 'gsoutput', 'rawpath']
    conf = {}
    for key in keys:
        conf[key] = exp[experiment]['datasets'][dsname][key] if key in exp[experiment]['datasets'][dsname] else exp[experiment]['datasets']['global'][key]
    if conf['enddate'] == 'yesterday':
        conf['enddate'] = (dt.datetime.today() - dt.timedelta(days=1)).strftime('%Y%m%d')
    conf['experiment'] = experiment
    conf['dsname'] = dsname

    gspath = os.path.join(conf['gsoutput'], experiment, dsname)
    datapath = os.path.join(conf['rawpath'], experiment, dsname)
    if os.path.exists(datapath):
        ans = utils.yes_or_no('This extraction will overwrite the dataset at {}. Are you sure?'.format(datapath))
        if not ans:
            return
    else:
        os.makedirs(datapath)

    try:
        client = bq.Client(project=conf['project'])

        # create dataset
        ds = client.dataset(conf['dataset'])
        if not ds.exists():
            ds.location = 'EU'
            ds.create()

        # if hddump, skip the db queries and dump the files already stored at GCS
        if hddump and hddump == 'True':
            tablenames = utils.get_table_names(conf)
            utils.extract_dataset_to_disk(datapath, tablenames, conf['project'], gspath)
            logger.info('Finished! Data dumped to disk at {}!'.format(datapath))
            return

        # randomly sample users who are active on the last week
        user_table_tmp, jobs = fetch_user_samples(ds, client, conf)
        wait_for_jobs(jobs)

        # make samples distinct
        jobs = distinct_users(user_table_tmp, conf['project'], ds, client)
        wait_for_jobs(jobs)

        # build feature set
        ft_tables, jobs = fetch_features(user_table_tmp, ds, client, conf)
        wait_for_jobs(jobs)

        # calculate the churn label for each user and each timestep
        user_table, jobs = calculate_churn(user_table_tmp, ds, client, conf)
        wait_for_jobs(jobs)
        user_table_tmp.delete()

        # backfill missing users on feature tables
        jobs = backfill_missing_users(user_table, ds, client, conf)
        wait_for_jobs(jobs)

        # serialize features
        tables = ft_tables[-(conf['obsdays']*conf['timesplits']):] + [user_table]
        jobs = dump_features_to_gcs(tables, gspath, client)

        # dump to disk
        wait_for_jobs(jobs)
        tablenames = [t.name for t in tables]
        utils.extract_dataset_to_disk(datapath, tablenames, conf['project'], gspath)
        with open(os.path.join(datapath, 'conf.json'), 'w') as f:
            json.dump(conf, f)

    except Exception as e:
        logger.exception(e)
        logger.info('Removing folder {}...'.format(datapath))
        shutil.rmtree(datapath)
        raise e

    logger.info('Finished! Data dumped to {}'.format(datapath))



def fetch_user_samples(ds, client, conf):
    """ Fetch a random sample of users based on a user_id hash """

    logger.info('Fetching user samples...')

    totaldays = conf['obsdays'] + conf['preddays']

    user_table = ds.table(name='users_{}_{}_tmp'.format(conf['experiment'], conf['dsname']))
    if user_table.exists():
        user_table.delete()

    #currdate = dt.datetime.strptime(enddate, '%Y%m%d')
    #currdate = dt.datetime.strptime(conf['enddate'], '%Y%m%d') - dt.timedelta(days=totaldays - 1)
    currdate = dt.datetime.strptime(conf['enddate']+'000000 -0500', '%Y%m%d%H%M%S %z') - dt.timedelta(days=totaldays - 1)
    jobs = []
    for i in range(min(conf['obsdays'], 7)):
        datestr = currdate.strftime('%Y%m%d')
        currdate_end = currdate + dt.timedelta(seconds=SECS_IN_DAY)

        # select a random sample of users based on a user_id hash and some wanted features
        query = """\
            SELECT uss.user_id as user_id
                FROM [activation-insights:sessions.features_{date}] as uss
                JOIN [business-critical-data:user_snapshot.user_snapshot_{date}] as usn
                ON uss.user_id = usn.user_id
                WHERE uss.consumption_time > 0.0 AND uss.platform IN ("android","android-tablet")
                    AND ABS(HASH(uss.user_id)) % {share} == 0 AND usn.reporting_country IN ("BR", "US", "MX")
                    AND TIMESTAMP_TO_MSEC(TIMESTAMP(uss.start_time)) >= {startslot} AND TIMESTAMP_TO_MSEC(TIMESTAMP(uss.start_time)) < {endslot}
        """.format(date=datestr, share=int(1 / conf['shareusers']),
                    startslot=int(currdate.timestamp()*1000), endslot=int(currdate_end.timestamp()*1000))

        logger.info('Selecting a random sample of users ({} of users on {})'.format(conf['shareusers'], datestr))

        jobname = 'user_ids_sampled_job_' + str(uuid.uuid4())
        job = client.run_async_query(jobname, query)
        job.destination = user_table
        job.allow_large_results = True
        job.write_disposition = 'WRITE_APPEND'
        job.begin()

        jobs.append(job)

        currdate = currdate + dt.timedelta(days=1)

    return user_table, jobs


def distinct_users(user_table, project, ds, client):
    """ Distinct the user table """

    query = """\
       SELECT user_id FROM [{project}:{dataset}.{table}]
        GROUP BY user_id\
    """.format(project=project, dataset=ds.name, table=user_table.name)

    logger.info('Fetching distinct user ids from samples...')

    jobname = 'user_distinct_samples_job_' + str(uuid.uuid4())
    job = client.run_async_query(jobname, query)
    job.destination = user_table
    job.allow_large_results = True
    job.write_disposition = 'WRITE_TRUNCATE'
    job.begin()

    return [job]


def fetch_features(user_table, ds, client, conf):
    """ Fetch the features for each user session, splitting into timesplits """

    jobs = []
    currdate = dt.datetime.strptime(conf['enddate']+'000000 -0500', '%Y%m%d%H%M%S %z')

    totaldays = conf['obsdays'] + conf['preddays']
    timesplits = conf['timesplits']

    ft_tables = []
    ft_table_name = 'features_{}_{}_'.format(conf['experiment'], conf['dsname'])
    for i in range(totaldays):

        # calculate the time splits
        currtimeslot_start = currdate.timestamp()
        datestr = currdate.strftime('%Y%m%d')

        logger.info('Fetching features for day {}'.format(datestr))

        for j in range(timesplits):
            if j+1 != timesplits:
                # calculate the timeslot range
                currtimeslot_end = currtimeslot_start + math.ceil(SECS_IN_DAY / timesplits) + 0.999
            else:
                # get the remaining secs lost due to rounding on the last slot. Will it be biased?
                currtimeslot_end = currdate.timestamp() + SECS_IN_DAY + 0.999

            # dump the features table
            ft_table = ds.table(name=ft_table_name+'{split}_{currdate}'.format(currdate=datestr, split=j))
            if ft_table.exists():
                ft_table.delete()

            # query user features
            query = """\
                SELECT user_id, {timesplit} as time, avg(consumption_time) as consumption_time, ifnull(stddev(consumption_time),0) as consumption_time_std, avg(session_length) as session_length, ifnull(stddev(session_length),0) as session_length_std,\
                         avg(skip_ratio) as skip_ratio, ifnull(stddev(skip_ratio),0) as skip_ratio_std, avg(unique_pages) as unique_pages, ifnull(stddev(unique_pages),0) as unique_pages_std, false as backfill FROM\
                        (SELECT user_id, consumption_time/1000 as consumption_time, session_length, skip_ratio, unique_pages\
                            FROM [activation-insights:sessions.features_{date}]\
                            WHERE user_id IN (SELECT user_id FROM [{project}:{dataset}.{table}])\
                                AND TIMESTAMP_TO_MSEC(TIMESTAMP(start_time)) >= {startslot} AND TIMESTAMP_TO_MSEC(TIMESTAMP(start_time)) < {endslot}
                                AND session_length > 0.0)
                GROUP BY user_id\
            """.format(date=datestr, project=conf['project'], dataset=ds.name, table=user_table.name,
                    startslot=int(currtimeslot_start*1000), endslot=int(currtimeslot_end*1000),
                    timesplit=currtimeslot_start)

            jobname = 'features_job_' + str(uuid.uuid4())
            job = client.run_async_query(jobname, query)
            job.destination = ft_table
            job.allow_large_results = True
            job.write_disposition = 'WRITE_APPEND'
            job.begin()

            jobs.append(job)
            ft_tables.append(ft_table)

            currtimeslot_start += math.ceil(SECS_IN_DAY / timesplits)

        currdate = currdate - dt.timedelta(days=1)

    return ft_tables, jobs


def backfill_missing_users(user_table, ds, client, conf):
    """ add the remaining missing users to the feature tables who did in fact churn """

    jobs = []
    enddate = conf['enddate']
    obsdays = conf['obsdays']
    preddays = conf['preddays']
    timesplits = conf['timesplits']
    project = conf['project']

    currdate = dt.datetime.strptime(enddate+'000000 -0500', '%Y%m%d%H%M%S %z') - dt.timedelta(days=obsdays + preddays - 1)

    ft_table_name = 'features_{}_{}_'.format(conf['experiment'], conf['dsname'])
    for i in range(obsdays):

        # calculate the time splits
        datestr = currdate.strftime('%Y%m%d')
        currtimeslot_start = currdate.timestamp()

        logger.info('Backfilling features for day {}'.format(datestr))

        for j in range(timesplits):

            ft_table = ds.table(name=ft_table_name+'{split}_{currdate}'.format(currdate=datestr, split=j))

            # append missing values to feature table
            query = """\
                SELECT user_id, {timesplit} as time, 0.0 as consumption_time, 0.0 as consumption_time_std, 0.0 as session_length, 0.0 as session_length_std,\
                            0.0 as skip_ratio, 0.0 as skip_ratio_std, 0.0 as unique_pages, 0.0 as unique_pages_std, true as backfill\
                            FROM [{project}:{dataset}.{utable}]\
                            WHERE user_id NOT IN (SELECT user_id FROM [{project}:{dataset}.{ftable}])\
            """.format(project=project, dataset=ds.name, ftable=ft_table.name, timesplit=currtimeslot_start, ds=conf['dsname'], utable=user_table.name)

            jobname = 'backfill_missing_features_job_' + str(uuid.uuid4())
            job = client.run_async_query(jobname, query)
            job.destination = ft_table
            job.allow_large_results = True
            job.write_disposition = 'WRITE_APPEND'
            job.begin()

            jobs.append(job)

            currtimeslot_start += math.ceil(SECS_IN_DAY / timesplits)

        currdate = currdate + dt.timedelta(days=1)

    return jobs


def calculate_churn(user_table_tmp, ds, client, conf):
    """ Calculate the users that will churn by checking the churn window """
    logger.info('Calculating churning and non-churning labels...')

    jobs = []

    preddays = conf['preddays']
    enddate = conf['enddate']
    timesplits = conf['timesplits']
    churnstart = dt.datetime.strptime(enddate+'000000 -0500', '%Y%m%d%H%M%S %z') - dt.timedelta(days=preddays - 1)

    user_table = ds.table(name='users_{}_{}'.format(conf['experiment'], conf['dsname']))
    if user_table.exists():
        user_table.delete()

    # build the query part that will join all users who had streams in the churn window
    union_query = ''
    for k in range(preddays):
        datestr_2 = (churnstart + dt.timedelta(days=k)).strftime('%Y%m%d')

        for l in range(timesplits):
            union_query += """select user_id from `{project}.{dataset}.features_{exp}_{ds}_{split}_{currdate}`
                        where consumption_time > 0.0 union distinct """.format(exp=conf['experiment'], split=l,
                                currdate=datestr_2, project=conf['project'], dataset=ds.name, ds=conf['dsname'])

    union_query = union_query[:-16]

    # append sessions from users that did not stream in this timesplit but who are also not churners
    query = """\
        select user_id, 0 as churn
            from `{project}.{dataset}.{table}`
            where user_id in ({union})
    """.format(project=conf['project'], dataset=ds.name, table=user_table_tmp.name, union=union_query)

    jobname = 'retained_features_job_' + str(uuid.uuid4())
    job = client.run_async_query(jobname, query)
    job.destination = user_table
    job.allow_large_results = True
    job.write_disposition = 'WRITE_APPEND'
    job.use_legacy_sql = False
    job.begin()
    jobs.append(job)

    # append sessions from users that did not stream in this timesplit but who are also not churners
    query = """\
        select user_id, 1 as churn
            from `{project}.{dataset}.{table}`
            where user_id not in ({union})
    """.format(project=conf['project'], dataset=ds.name, table=user_table_tmp.name, union=union_query)

    jobname = 'retained_features_job_' + str(uuid.uuid4())
    job = client.run_async_query(jobname, query)
    job.destination = user_table
    job.allow_large_results = True
    job.write_disposition = 'WRITE_APPEND'
    job.use_legacy_sql = False
    job.begin()
    jobs.append(job)

    return user_table, jobs


def dump_features_to_gcs(ft_tables, dest, client):
    """ Dump generated tables as files on Google Cloud Storage"""

    logger.info('Dumping {} tables to {}...'.format(len(ft_tables), dest))

    jobs = []
    for ft_table in ft_tables:
        path = dest + '/' + ft_table.name
        jobname = 'features_dump_job_' + str(uuid.uuid4())
        job = client.extract_table_to_storage(jobname, ft_table, path)
        job.begin()

        jobs.append(job)

    return jobs


def wait_for_jobs(jobs):
    """ wait for async GCP jobs to finish """
    logger.info("Waiting for {} jobs to finish...".format(len(jobs)))

    import time
    cntr = 0
    for job in jobs:
        if cntr % 10 == 0:
            logger.info('{} out of {} jobs finished...'.format(cntr, len(jobs)))
        cntr += 1

        numtries = 9000
        job.reload()
        while job.state != 'DONE':
            time.sleep(5)
            job.reload()
            numtries -= 1

            if numtries == 0:
                logger.error('Job {} timed out! Aborting!'.format(job.name))
                raise Exception('Async job timed out')

        if job.errors:
            logger.error('Job {} failed! Aborting!'.format(job.name))
            raise Exception('Async job failed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Data extracter')
    parser.add_argument('--exppath', default='../../experiments.json', help='Path to the experiments json file')
    parser.add_argument('--experiment', default='temporal_static', help='Name of the experiment being performed')
    parser.add_argument('--dsname', default='session_6030d', help='Name of the dataset being transformed')
    parser.add_argument('--hddump', default='False', help='If True, jump straight to the data import from GCS')

    args = parser.parse_args()

    main(exppath=args.exppath, experiment=args.experiment, dsname=args.dsname, hddump=args.hddump)
