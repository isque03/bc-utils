import calendar
import enum
import io
import json
import logging
import os
import os.path
import pytz
import re
import time
import traceback
import urllib.parse
from datetime import datetime, timedelta
from itertools import cycle
from pathlib import Path
from random import randint

import pandas as pd
import requests
from bs4 import BeautifulSoup

from bcutils.config import CONTRACT_MAP

logger = logging.getLogger(__name__)


class HistoricalDataResult(enum.Enum):
    NONE = 1
    OK = 2
    EXISTS = 3
    EXCEED = 4
    INSUFFICIENT = 5


class Resolution(enum.Enum):
    Day = 1
    Hour = 2


class BCException(Exception):
    pass


class IntegrityException(Exception):
    pass


class RecentUpdateException(Exception):
    pass


class EmptyDataException(Exception):
    pass


MONTH_LIST = ["F", "G", "H", "J", "K", "M", "N", "Q", "U", "V", "X", "Z"]
BARCHART_URL = "https://www.barchart.com/"


def create_bc_session(config_obj: dict, do_login=True):
    # start a session
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    if (
        do_login is True
        and "barchart_username" not in config_obj
        or "barchart_password" not in config_obj
    ):
        raise Exception("Barchart credentials are required")

    if do_login:
        # GET the login page, scrape to get CSRF token
        resp = session.get(BARCHART_URL + "login")
        soup = BeautifulSoup(resp.text, "html.parser")
        tag = soup.find(type="hidden")
        csrf_token = tag.attrs["value"]
        logger.info(
            f"GET {BARCHART_URL + 'login'}, status: {resp.status_code}, "
            f"CSRF token: {csrf_token}"
        )

        # login to site with a POST
        payload = {
            "email": config_obj["barchart_username"],
            "password": config_obj["barchart_password"],
            "_token": csrf_token,
        }
        resp = session.post(BARCHART_URL + "login", data=payload)
        logger.info(f"POST {BARCHART_URL + 'login'}, status: {resp.status_code}")
        if resp.url == BARCHART_URL + "login":
            raise Exception("Invalid Barchart credentials")

    return session


def save_prices_for_contract(
    session,
    contract,
    save_path,
    start_date,
    end_date,
    dry_run=False,
):
    period = _get_period(save_path)

    try:
        # do we have this file already?
        if os.path.isfile(save_path):
            logger.info(
                f"{period} data for contract '{contract}' already downloaded "
                f"({save_path}) - skipping\n"
            )
            return HistoricalDataResult.EXISTS

        # before we attempt to download hourly data, check there is some
        if period == "hourly" and _insufficient_hourly_data(session, contract):
            logger.info(f"Insufficient hourly data for '{contract}' - skipping\n")
            return HistoricalDataResult.INSUFFICIENT

    except Exception as e:  # skipcq broad by design
        logger.error(f"Problem: {e}, {traceback.format_exc()}")

    logger.info(
        f"getting historic {period} prices for contract '{contract}', "
        f"from {start_date.strftime('%Y-%m-%d')} "
        f"to {end_date.strftime('%Y-%m-%d')}"
    )

    try:
        # open historic data download page for required contract
        url = f"{BARCHART_URL}futures/quotes/{contract}/historical-download"
        hist_resp = session.get(url)
        logger.info(f"GET {url}, status {hist_resp.status_code}")

        if hist_resp.status_code != 200:
            logger.info(f"No downloadable data found for contract '{contract}'\n")
            return HistoricalDataResult.NONE

        xsrf = urllib.parse.unquote(hist_resp.cookies["XSRF-TOKEN"])

        # scrape page for csrf_token
        hist_soup = BeautifulSoup(hist_resp.text, "html.parser")
        hist_tag = hist_soup.find(name="meta", attrs={"name": "csrf-token"})
        hist_csrf_token = hist_tag.attrs["content"]

        # check allowance
        payload = {"onlyCheckPermissions": "true"}
        headers = {
            "content-type": "application/x-www-form-urlencoded",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": url,
            "x-xsrf-token": xsrf,
        }
        resp = session.post(BARCHART_URL + "my/download", headers=headers, data=payload)

        allowance = json.loads(resp.text)

        if allowance.get("error") is not None:
            return HistoricalDataResult.EXCEED

        if allowance["success"]:
            logger.info(
                f"POST {BARCHART_URL + 'my/download'}, "
                f"status: {resp.status_code}, "
                f"allowance success: {allowance['success']}, "
                f"allowance count: {allowance['count']}"
            )

            # download data
            xsrf = urllib.parse.unquote(resp.cookies["XSRF-TOKEN"])
            headers = {
                "content-type": "application/x-www-form-urlencoded",
                "Accept-Encoding": "gzip, deflate, br",
                "Referer": url,
                "x-xsrf-token": xsrf,
            }

            payload = {
                "_token": hist_csrf_token,
                "fileName": contract + "_Daily_Historical Data",
                "symbol": contract,
                "fields": "tradeTime.format(Y-m-d),openPrice,highPrice,lowPrice,"
                "lastPrice,volume",
                "startDate": start_date.strftime("%Y-%m-%d"),
                "endDate": end_date.strftime("%Y-%m-%d"),
                "orderBy": "tradeTime",
                "orderDir": "asc",
                "method": "historical",
                "limit": "20000",
                "customView": "true",
                "pageTitle": "Historical Data",
            }

            if period == "daily":
                payload["type"] = "eod"
                payload["period"] = "daily"
                dateformat = "%Y-%m-%d"

            if period == "hourly":
                payload["type"] = "minutes"
                payload["interval"] = 60
                dateformat = "%m/%d/%Y %H:%M"

            if not dry_run:
                resp = session.post(
                    BARCHART_URL + "my/download", headers=headers, data=payload
                )
                logger.info(
                    f"POST {BARCHART_URL + 'my/download'}, "
                    f"status: {resp.status_code}, "
                    f"data length: {len(resp.content)}"
                )
                if resp.status_code == 200:
                    if "Error retrieving data" not in resp.text:
                        iostr = io.StringIO(resp.text)
                        df = pd.read_csv(iostr, skipfooter=1, engine="python")
                        df["Time"] = pd.to_datetime(df["Time"], format=dateformat)
                        df.set_index("Time", inplace=True)
                        df.index = df.index.tz_localize(tz="US/Central").tz_convert(
                            "UTC"
                        )
                        df = df.rename(columns={"Last": "Close"})

                        logger.info(f"writing to: {save_path}")
                        df.to_csv(save_path, date_format="%Y-%m-%dT%H:%M:%S%z")

                    else:
                        logger.info(
                            f"Barchart data problem for '{contract}', not writing"
                        )
            else:
                logger.info(f"Not POSTing to {BARCHART_URL + 'my/download'}, dry_run")

            logger.info(
                f"Finished getting Barchart historic {period} prices for {contract}\n"
            )

        return HistoricalDataResult.OK

    except Exception as e:  # skipcq broad by design
        logger.error(f"Error {e}")


def get_barchart_downloads(
    session,
    contract_map=None,
    contract_list=None,
    instr_list=None,
    save_dir=None,
    start_year=1950,
    end_year=2025,
    dry_run=False,
    do_daily=True,
    pause_between_downloads=True,
):
    if contract_map is None:
        contract_map = CONTRACT_MAP

    inv_contract_map = _build_inverse_map(contract_map)

    max_exceeded = False

    try:
        if contract_list is None:
            contract_list = _build_contract_list(
                start_year, end_year, instr_list=instr_list, contract_map=contract_map
            )

        for contract in contract_list:
            if max_exceeded:
                break

            for resolution in ["Hour", "Day"] if do_daily else ["Hour"]:
                # work out instrument code and get config
                market_code = contract[: len(contract) - 3]
                instr_code = inv_contract_map[market_code.upper()]
                instr_config = contract_map[instr_code]

                # get contract month and year
                month, year = _get_contract_month_year(contract)

                # build save path
                save_path = _build_save_path(
                    instr_code, month, year, resolution, save_dir
                )

                # calculate date range
                start_date, end_date = _get_start_end_dates(month, year, instr_config)

                if _before_tick_date(resolution, start_date, instr_config):
                    logger.info(
                        f"Hourly prices for {contract} starting "
                        f"{start_date.strftime('%Y-%m-%d')} is before configured "
                        f"tick date - skipping\n"
                    )
                    continue

                # download and save
                result = save_prices_for_contract(
                    session,
                    contract,
                    save_path,
                    start_date,
                    end_date,
                    dry_run=dry_run,
                )

                if result in [
                    HistoricalDataResult.EXISTS,
                    HistoricalDataResult.NONE,
                    HistoricalDataResult.INSUFFICIENT,
                ]:
                    continue
                elif result == HistoricalDataResult.EXCEED:
                    logger.info("Max daily download reached, aborting")
                    max_exceeded = True
                    break
                else:
                    if pause_between_downloads:
                        # cursory attempt to not appear like a bot
                        time.sleep(0 if dry_run else randint(7, 15))

        # logout
        resp = session.get(BARCHART_URL + "logout", timeout=10)
        logger.info(f"GET {BARCHART_URL + 'logout'}, status: {resp.status_code}")

    except Exception as e:  # skipcq broad by design
        logger.error(f"Error {e}")
        traceback.print_exc()


def update_barchart_downloads(
    instr_code="GOLD", save_dir=None, days_ago=400, dry_run=False
):
    if days_ago is None:
        days_ago = 360

    from_date = datetime.now() - timedelta(days=days_ago)

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    check_integrity_list = []

    for resol in ["Hour", "Day"]:
        regex = re.compile("^" + resol + "_" + instr_code + "_[0-9]{8}.csv")

        file_names = [fn for fn in os.listdir(save_dir) if regex.match(fn)]

        for file in file_names:
            instr_code = _instr_code_from_file_name(file)
            contract_date = _contract_date_from_file_name(file)
            contract_id = _get_barchart_id(
                instr_code, contract_date.year, contract_date.month
            )

            if contract_date > from_date:
                if dry_run:
                    print(f"DRY RUN: would update contract {contract_id}, file {file}")
                else:
                    try:
                        update_barchart_contract_file(
                            session, save_dir, contract_id, resol
                        )
                    except IntegrityException:
                        logging.error(f"File index problem with {file}, please check")
                        check_integrity_list.append(file)
                    except RecentUpdateException:
                        logging.warning(f"Skipping {contract_id}, recently updated")
                    except EmptyDataException:
                        logging.info(f"Empty data for {contract_id}")

    if len(check_integrity_list) > 0:
        print(f"These files have integrity problems: {check_integrity_list}")


def update_barchart_contract_file(session, path, contract_id, resol="Hour"):
    inv_contract_map = _build_inverse_map(CONTRACT_MAP)

    file = _filename_from_barchart_id(contract_id, inv_contract_map, resol)
    instr_code = _instr_code_from_file_name(file)

    now = datetime.now().astimezone(tz=pytz.utc)

    input_path = f"{path}/{file}"
    logging.info(f"Starting update for {input_path}...")

    existing = pd.read_csv(input_path)
    existing["Time"] = pd.to_datetime(existing["Time"], format="%Y-%m-%dT%H:%M:%S%z")
    try:
        existing.set_index("Time", inplace=True, verify_integrity=True)
        last_index_date = existing.index[-1]
    except ValueError:
        raise IntegrityException(f"Index problem with {file}, needs manual check")

    if (now - last_index_date).days < 4:
        raise RecentUpdateException(f"Skipping {file}, recently updated")

    logging.info(
        f"Instrument: {instr_code}, contract: {contract_id}, "
        f"last entry: {last_index_date}"
    )

    if resol == "Hour":
        period = "hourly"
    elif resol == "Day":
        period = "daily"
    else:
        raise Exception(f"Unexpected resolution: {resol}")

    update = get_historical_prices_for_contract(session, contract_id, Resolution[resol])
    if period == "hourly":
        start = last_index_date + timedelta(hours=1)
    else:
        start = last_index_date + timedelta(hours=25)
    end = now - timedelta(days=2)

    if update is not None:
        logging.info(
            f"Adding new rows from {start.strftime('%Y-%m-%d')} to "
            f"{end.strftime('%Y-%m-%d')}"
        )
        update = update[start:end]

        try:
            final = pd.concat([existing, update], verify_integrity=True)
            output_path = f"{path}/{file}"
            final.to_csv(output_path, date_format="%Y-%m-%dT%H:%M:%S%z")
        except Exception as ex:
            logging.warning(f"Problem with {file}: {ex}")
    else:
        raise EmptyDataException(f"Empty data for {contract_id}")


def get_historical_prices_for_contract(
    session, instr_symbol: str, resolution: Resolution = Resolution.Day
) -> pd.DataFrame:
    """
    Get historical price data

    :param session: session
    :type session: requests.Session
    :param instr_symbol: Barchart contract symbol e.g. GCM21
    :type instr_symbol: str
    :param resolution: frequency of price data requested
    :type resolution: Resolution, 'Day' or 'Hour'
    :return: df
    :rtype: pandas DataFrame
    """

    assert instr_symbol

    try:
        # GET the futures quote chart page, scrape to get XSRF token
        # https://www.barchart.com/futures/quotes/GCM21/interactive-chart
        chart_url = BARCHART_URL + f"futures/quotes/{instr_symbol}/interactive-chart"
        chart_resp = session.get(chart_url)
        xsrf = urllib.parse.unquote(chart_resp.cookies["XSRF-TOKEN"])

        headers = {
            "content-type": "text/plain; charset=UTF-8",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": chart_url,
            "x-xsrf-token": xsrf,
        }

        payload = {
            "symbol": instr_symbol,
            "maxrecords": "640",
            "volume": "contract",
            "order": "asc",
            "dividends": "false",
            "backadjust": "false",
            "days to expiration": "1",
            "contractroll": "combined",
        }

        if resolution == Resolution.Day:
            data_url = BARCHART_URL + "proxies/timeseries/queryeod.ashx"
            payload["data"] = "daily"
            payload["contractroll"] = "expiration"
        else:
            data_url = BARCHART_URL + "proxies/timeseries/queryminutes.ashx"
            payload["interval"] = "60"
            payload["contractroll"] = "combined"

        # get prices for instrument from BC internal API
        prices_resp = session.get(data_url, headers=headers, params=payload)
        ratelimit = prices_resp.headers["x-ratelimit-remaining"]
        if int(ratelimit) <= 15:
            time.sleep(20)
        logger.info(
            f"GET {data_url} {instr_symbol}, {prices_resp.status_code}, "
            f"ratelimit {ratelimit}"
        )

        # read response into dataframe
        iostr = io.StringIO(prices_resp.text)
        df = pd.read_csv(iostr, header=None)

        # convert to expected format
        price_data_as_df = _raw_barchart_data_to_df(df, bar_freq=resolution)

        if len(df) == 0:
            raise BCException(
                f"Zero length Barchart price data found for {instr_symbol}"
            )

        logger.debug(f"Latest price {df.index[-1]} with {resolution}")

        return price_data_as_df

    except Exception as ex:
        logger.error(f"Problem getting historical data: {ex}")
        raise BCException from ex


def _build_contract_list(start_year, end_year, instr_list=None, contract_map=None):
    contracts_per_instrument = {}
    contract_list = []
    count = 0

    if contract_map is None:
        contract_map = CONTRACT_MAP

    if instr_list is None:
        instr_list = contract_map.keys()

    for instr in instr_list:
        config_obj = contract_map[instr]
        futures_code = config_obj["code"]
        if futures_code == "none":
            continue
        rollcycle = config_obj["cycle"]
        instrument_list = []

        for year in range(start_year, end_year):
            for month_code in list(rollcycle):
                instrument_list.append(
                    f"{futures_code}{month_code}{str(year)[len(str(year))-2:]}"
                )
        contracts_per_instrument[instr] = instrument_list
        count = count + len(instrument_list)

    logger.info(f"Contract count: {count}")

    pool = cycle(contract_map.keys())

    while len(contract_list) < count:
        try:
            instr = next(pool)
        except StopIteration:
            continue
        if instr not in contracts_per_instrument:
            continue
        instr_list = contracts_per_instrument[instr]
        config_obj = contract_map[instr]
        rollcycle = config_obj["cycle"]
        if len(rollcycle) > 10:
            max_count = 3
        elif len(rollcycle) > 7:
            max_count = 2
        else:
            max_count = 1

        for _ in range(0, max_count):
            if len(instr_list) > 0:
                contract_list.append(instr_list.pop())

    # return ['CTH21', 'CTK21', 'CTN21', 'CTU21', 'CTZ21', 'CTH22']

    logger.info(f"Contract list: {contract_list}")
    return contract_list


def _build_inverse_map(contract_map):
    return {v["code"]: k for k, v in contract_map.items()}


def _before_tick_date(resolution, start_date, instr_config):
    if "tick_date" in instr_config:
        tick_date = datetime.strptime(instr_config["tick_date"], "%Y-%m-%d")
    else:
        tick_date = None

    return resolution == "Hour" and tick_date is not None and start_date < tick_date


def _get_overview(session, contract_id):
    """
    GET the futures overview page, e.g.
        https://www.barchart.com/futures/quotes/B6M21/overview
    :param contract_id: contract identifier
    :type contract_id: str
    :return: resp
    :rtype: HTTP response object
    """
    url = BARCHART_URL + "futures/quotes/%s/overview" % contract_id
    resp = session.get(url)
    logger.debug(f"GET {url}, response {resp.status_code}")
    return resp


def _build_save_path(instr_code, month, year, resolution, save_directory):
    if save_directory is None:
        download_dir = os.getcwd()
    else:
        download_dir = save_directory
    datecode = str(year) + "{0:02d}".format(month)
    filename = f"{resolution}_{instr_code}_{datecode}00.csv"
    save_path = f"{download_dir}/{filename}"
    return save_path


def _get_contract_month_year(contract):
    year_code = int(contract[len(contract) - 2 :])
    month_code = contract[len(contract) - 3]
    if year_code > 30:
        year = 1900 + year_code
    else:
        year = 2000 + year_code
    month = _month_from_contract_letter(month_code.upper())
    return month, year


def _insufficient_hourly_data(session, symbol):
    try:
        df = get_historical_prices_for_contract(session, symbol, Resolution.Hour)
        return len(df) < 30
    except Exception:  # skipcq broad by design
        return True


def _get_start_end_dates(month, year, instr_config=None):
    now = datetime.now()

    if instr_config and "days_count" in instr_config:
        day_count = instr_config["days_count"]
    else:
        day_count = 120

    # we need to work out a date range for which we want the prices
    # for expired contracts the end date would be the expiry date;
    # for KISS sake, lets assume expiry is last date of contract month
    end_date = datetime(year, month, calendar.monthrange(year, month)[1])

    # but, if that end_date is in the future, then we may as well make it today...
    if now.date() < end_date.date():
        end_date = now

    # assumption no.2: lets set start date at <day_count> days before end date
    day_count = timedelta(days=day_count)
    start_date = end_date - day_count

    return start_date, end_date


def _month_from_contract_letter(contract_letter):
    """
    Returns month number (1 is January) from contract letter

    :param contract_letter:
    :return:
    """
    try:
        month_number = MONTH_LIST.index(contract_letter)
    except ValueError:
        return None

    return month_number + 1


def _get_period(save_path):
    path_obj = Path(save_path)
    resol = path_obj.name.split("_")[0]
    if resol == "Hour":
        period = "hourly"
    elif resol == "Day":
        period = "daily"
    else:
        raise Exception(f"Unexpected resolution: {resol}")
    return period


def _raw_barchart_data_to_df(
    price_data_raw: pd.DataFrame,
    bar_freq: Resolution = Resolution.Day,
) -> pd.DataFrame:
    if price_data_raw is None:
        logging.warning("No historical price data from Barchart")
        return None

    if bar_freq == Resolution.Day:
        dateformat = "%Y-%m-%d"
        col_no = 1
        cols_to_remove = [0, 1, 6]
    else:
        dateformat = "%Y-%m-%d %H:%M"
        col_no = 0
        cols_to_remove = [0, 1]

    price_data_raw["Date"] = pd.to_datetime(price_data_raw[col_no], format=dateformat)
    price_data_raw.set_index("Date", inplace=True)
    price_data_raw.index = price_data_raw.index.tz_localize(tz="US/Central").tz_convert(
        "UTC"
    )
    price_data_raw.index.name = "Time"
    df = price_data_raw.drop(columns=cols_to_remove)
    df.columns = ["Open", "High", "Low", "Close", "Volume"]

    return df


def _get_barchart_id(instr, year, month):
    instr_config = CONTRACT_MAP[instr]
    bc_instr = instr_config["code"]
    month_code = MONTH_LIST[month - 1]
    year_sub = year - 2000 if year > 2000 else year - 1900
    bc_id = f"{bc_instr}{month_code}{year_sub}"
    return bc_id


def _instr_code_from_file_name(file_name):
    instr_code = file_name[file_name.find("_") + 1 : file_name.rfind("_")]
    return instr_code


def _contract_date_from_file_name(file_name):
    date_str = file_name[-12:-4]
    logging.debug(f"file: {file_name}, date_str: {date_str}")
    contract_date = datetime.strptime(f"{date_str[:-2]}01", "%Y%m01")
    return contract_date


def _filename_from_barchart_id(contract_id, inv_map, resol):
    year_code = int(contract_id[len(contract_id) - 2 :])
    month_code = contract_id[len(contract_id) - 3]
    if year_code > 30:
        year = 1900 + year_code
    else:
        year = 2000 + year_code
    month = _month_from_contract_letter(month_code.upper())
    market_code = contract_id[: len(contract_id) - 3]
    instrument = inv_map[market_code.upper()]
    datecode = str(year) + "{0:02d}".format(month)
    filename = f"{resol}_{instrument}_{datecode}00.csv"
    return filename


def _env():
    credentials = {
        "barchart_username": "BARCHART_USERNAME",
        "barchart_password": "BARCHART_PASSWORD",
    }
    barchart_config = {
        k: os.environ.get(v) for k, v in credentials.items() if v in os.environ
    }
    return barchart_config


if __name__ == "__main__":
    get_barchart_downloads(
        create_bc_session(config_obj=_env()),
        contract_map={
            "AUD": {"code": "A6", "cycle": "HMUZ", "tick_date": "2009-11-24"}
        },
        save_dir="/home/user/barchart_data",
        start_year=2020,
        end_year=2022,
        dry_run=False,
    )

    # do_barchart_updates(
    #     create_bc_session(config_obj=_env()),
    #     contract_map={
    #         "AUD": {"code": "A6", "cycle": "HMUZ", "tick_date": "2009-11-24"}
    #     },
    #     save_dir="/home/user/barchart_data",
    #     start_year=2020,
    #     end_year=2022,
    #     dry_run=False,
    # )
