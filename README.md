# Petrobel Digitalization dashboard (stand-alone, complete)

The **Digitalization** section of the Petrobel Well Operations Dashboard as a project of its own, with the same look and content, **plus** the data pipeline
that builds its data from the Excel files and an **Update data** tab to upload new versions of those files.

* Which running wells should get a **PT sensor, ROPC or VSD**. Only RUNNING wells are assessed (status from the most recent running-hours reading: not 0 = running).
* A running well whose last test has net oil at or below the threshold (default 4 m3/d) gets a PT sensor automatically. The other running wells are scored
  on net oil (10-30) and gross rate, NLAP, rod loaded, beam loading and gearbox torque (0-10 each): top 30% VSD, next 30% ROPC, rest PT sensors.
* Rod loaded, beam loading and gearbox torque are read from the "Dynamometer Test Results" text of the latest survey (manual entry and imported Echometer reports also work).

## Start it

Double-click **START_DASHBOARD.bat** (the first start installs the packages; Python 3.10 or newer; the file finds Python by itself).
Anywhere else: `pip install -r requirements.txt`, then in `01_DASHBOARD_APP`: `streamlit run app.py`.

## Update the data (two ways)

1. **In the dashboard:** open the **Update data** tab, upload the Excel files that changed (any file names, any order; each file is identified from its contents),
   check how they were identified and press *Save files and update dashboard*. Files you do not upload keep their previous version.
2. **From a folder:** drop the files into `02_UPLOAD_NEW_DATA` and double-click **Update_Data_From_Folder.bat**. The result is written to `00_START_HERE/Last_Update_Status.txt`.

The files it understands: **DATABASE** (well tests), **Monthly** (daily running hours), **Cumulative DFL** (fluid levels and dynamometer results),
**Wells Operation Mode** (the reference list of wells), and **Production Test Results / Water Cut Samples (SRP)**.

* A new SRP test sheet is compared well by well with the last test: the same test is ignored, a different one is added as a new test dated the upload day.
* Rates in barrels (BOPD / BFPD / bbl/d) are recognised and converted to m3/d; many spellings of gross and net are understood.
* An SRP sheet uploaded by mistake can be taken back in *Update data > Remove SRP well tests uploaded by mistake*.
* To protect the Update data tab, write a password in `00_START_HERE/Upload_Password.txt` (empty = open).

## How the data flows

```
03_DATA/SOURCE_FILES   the Excel files (the newest version of each)
        |  01_match_well_names.py   matches every well name to the Operation Mode wells  ->  Cleaned_*.xlsx
        v
03_DATA/PROCESSED      02_merge_data.py   ->  Master_Dashboard_Database.csv, Test_History.csv, DFL_History.csv, reports
        |
        v
dashboard (01_DASHBOARD_APP/app.py)
```

* **DATABASE:** only the cleaned copy (`Cleaned_DATABASE.xlsx`) was supplied, so it is used as it is until you upload a new DATABASE file.
* Unmatched well names are listed in `03_DATA/PROCESSED/Well_Matching_Report.xlsx`. Reviewed mappings go into `03_DATA/well_aliases.csv`
  (`raw_name,standard_well`). Copy your existing `well_aliases.csv` here if you have one.
* Old versions of replaced files are kept in `04_ARCHIVE` (60 days), logs in `05_LOGS`.

## Share it with the team

* **Shared drive / zip:** send the whole folder. Everyone runs START_DASHBOARD.bat. Updates made on one copy are not seen by the others.
* **One PC for everybody on the network:** in `01_DASHBOARD_APP` run `streamlit run app.py --server.address 0.0.0.0` and share `http://<that PC's name>:8501`.
  Then everyone sees the same data, and anyone with the password can update it.
* **GitHub + Streamlit Community Cloud:** keep the repository **private**: the files hold well production data. Updates made in the cloud app are lost when
  the server restarts, so commit the new data instead.

## What is different from the full dashboard

Only this section and the data update are here: no other tabs, no live-API page, no chat. The calculations are the same code.
