import os
import time
import datetime
import jwt
import requests
import argparse


"""
In this section, we define the helper functions to access the WindBorne API
This is described in https://windbornesystems.com/docs/api
"""


def wb_get_request(url):
    """
    Make a GET request to WindBorne, authorizing with WindBorne correctly
    """

    client_id = os.environ['WB_CLIENT_ID']  # Make sure to set this!
    api_key = os.environ['WB_API_KEY']  # Make sure to set this!

    # create a signed JSON Web Token for authentication
    # this token is safe to pass to other processes or servers if desired, as it does not expose the API key
    signed_token = jwt.encode({
        'client_id': client_id,
        'iat': int(time.time()),
    }, api_key, algorithm='HS256')

    # make the request, checking the status code to make sure it succeeded
    response = requests.get(url, auth=(client_id, signed_token))

    retries = 0
    while response.status_code == 502 and retries < 5:
        print("502 Bad Gateway, sleeping and retrying")
        time.sleep(2**retries)
        response = requests.get(url, auth=(client_id, signed_token))
        retries += 1

    response.raise_for_status()

    # return the response body
    return response.json()


"""
In this section, we have the core functions to convert data to csv
"""

def convert_to_csv(data, output_file='export.csv'):
    if len(data) == 0:
        print("No data provided; skipping")
        return

    lines = []
    keys = ['timestamp', 'time', 'latitude', 'longitude', 'altitude', 'humidity', 'mission_name', 'pressure', 'specific_humidity', 'speed_u', 'speed_v', 'temperature']

    # Write the header
    lines.append(','.join(keys))

    for i in range(len(data)):
        point = data[i]

        pieces = []

        point['time'] = datetime.datetime.fromtimestamp(point['timestamp'], tz=datetime.timezone.utc).isoformat()

        for key in keys:
            if key in point:
                pieces.append(str(point[key]))
            else:
                pieces.append('')

        lines.append(','.join(pieces))

    with open(output_file, 'w') as f:
        f.write('\n'.join(lines))



"""
In this section, we tie it all together, querying the WindBorne API and converting it to csv
"""

def output_data(accumulated_observations, mission_name, starttime, bucket_hours):
    accumulated_observations.sort(key=lambda x: x['timestamp'])

    # Here, set the earliest time of data to be the first observation time, then set it to the most recent
    #    start of a bucket increment.
    # The reason to do this rather than using the input starttime, is because sometimes the data
    #    doesn't start at the start time, and the underlying output would try to output data that doesn't exist
    #
    accumulated_observations.sort(key=lambda x: x['timestamp'])
    earliest_time = accumulated_observations[0]['timestamp']
    if (earliest_time < starttime):
        print("WTF, how can we have gotten data from before the starttime?")
    curtime = earliest_time - earliest_time % (bucket_hours * 60 * 60)

    start_index = 0
    for i in range(len(accumulated_observations)):
        if accumulated_observations[i]['timestamp'] - curtime > bucket_hours * 60 * 60:
            segment = accumulated_observations[start_index:i]
            mt = datetime.datetime.fromtimestamp(curtime, tz=datetime.timezone.utc)+datetime.timedelta(hours=bucket_hours/2)
            output_file = (f"WindBorne_%s_%04d-%02d-%02d_%02d_%dh.csv" %
                           (mission_name, mt.year, mt.month, mt.day, mt.hour, bucket_hours))

            convert_to_csv(segment, output_file)

            start_index = i
            curtime += datetime.timedelta(hours=bucket_hours).seconds

    # Cover any extra data within the latest partial bucket
    segment = accumulated_observations[start_index:]
    mt = datetime.datetime.fromtimestamp(curtime, tz=datetime.timezone.utc) + datetime.timedelta(hours=bucket_hours / 2)
    output_file = (f"WindBorne_%s_%04d-%02d-%02d_%02d_%dh.csv" %
                   (mission_name, mt.year, mt.month, mt.day, mt.hour, bucket_hours))
    convert_to_csv(segment, output_file)


def main():
    """
    Queries WindBorne API for data from the input time range and converts it to csv
    :return:
    """

    parser = argparse.ArgumentParser(description="""
    Retrieves WindBorne data and output to csv.
    
    Files will be broken up into time buckets as specified by the --bucket_hours option, 
    and the output file names will contain the time at the mid-point of the bucket. For 
    example, if you are looking to have files centered on say, 00 UTC 29 April, the start time
    should be 3 hours prior to 00 UTC, 21 UTC 28 April.
    """, formatter_class=argparse.RawTextHelpFormatter)

    parser.add_argument("times", nargs='+',
                        help='Starting and ending times to retrieve obs.  Format: YY-mm-dd_HH:MM '
                             'Ending time is optional, with current time used as default')
    parser.add_argument('-b', '--bucket_hours', type=float, default=6.0,
                        help='Number of hours of observations to accumulate into a file before opening the next file')
    parser.add_argument('-c', '--combine_missions', action='store_true',
                        help="If selected, all missions are combined in the same output file")
    args = parser.parse_args()

    if len(args.times) == 1:
        starttime=int(datetime.datetime.strptime(args.times[0], '%Y-%m-%d_%H:%M').
                      replace(tzinfo=datetime.timezone.utc).timestamp())
        endtime=int(datetime.datetime.now().timestamp())
    elif len(args.times) == 2:
        starttime=int(datetime.datetime.strptime(args.times[0], '%Y-%m-%d_%H:%M').
                      replace(tzinfo=datetime.timezone.utc).timestamp())
        endtime=int(datetime.datetime.strptime(args.times[1], '%Y-%m-%d_%H:%M').
                    replace(tzinfo=datetime.timezone.utc).timestamp())
    else:
        print("error processing input args, one or two arguments are needed")
        exit(1)

    if (not "WB_CLIENT_ID" in os.environ) or (not "WB_API_KEY" in os.environ) :
        print("  ERROR: You must set environment variables WB_CLIENT_ID and WB_API_KEY\n"
              "  If you don't have a client ID or API key, please contact WindBorne.")
        exit(1)

    args = parser.parse_args()
    bucket_hours = args.bucket_hours

    observations_by_mission = {}
    accumulated_observations = []
    has_next_page = True

    next_page = f"https://sensor-data.windbornesystems.com/api/v1/super_observations.json?min_time={starttime}&max_time={endtime}&include_mission_name=true"

    while has_next_page:
        # Note that we query superobservations, which are described here:
        # https://windbornesystems.com/docs/api#super_observations
        # We find that for most NWP applications this leads to better performance than overwhelming with high-res data
        observations_page = wb_get_request(next_page)
        has_next_page = observations_page["has_next_page"]
        if len(observations_page['observations']) == 0:
            print("Could not find any observations for the input date range")

        if has_next_page:
            next_page = observations_page["next_page"]+"&include_mission_name=true&min_time={}&max_time={}".format(starttime,endtime)

        print(f"Fetched page with {len(observations_page['observations'])} observation(s)")
        for observation in observations_page['observations']:
            if 'mission_name' not in observation:
                print("Warning: got an observation without a mission name")
                continue
                
            if observation['mission_name'] not in observations_by_mission:
                observations_by_mission[observation['mission_name']] = []

            observations_by_mission[observation['mission_name']].append(observation)
            accumulated_observations.append(observation)

            # alternatively, you could call `time.sleep(60)` and keep polling here
            # (though you'd have to move where you were calling convert_to_csv)


    if len(observations_by_mission) == 0:
        print("No observations found")
        return

    if args.combine_missions:
        mission_name = 'all'
        output_data(accumulated_observations, mission_name, starttime, bucket_hours)
    else:
        for mission_name, accumulated_observations in observations_by_mission.items():
            output_data(accumulated_observations, mission_name, starttime, bucket_hours)


if __name__ == '__main__':
    main()
