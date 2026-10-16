# Goal:
Prepare AFLOW materials data for integration into our database.

# Description:
Review AFLOW’s structure and available properties, collect and normalize its records, validate data quality, and present database statistics and the processing workflow in a short slide deck.


curl -fL --retry 3 \
  "https://aflow.org/API/aflux/?$(cat data/aflux_query.txt)" \
  -o data/aflow_data.json

  curl -fL --retry 3 \
        'https://aflow.org/API/aflux/?help(properties)' \
        -o aflow_properties.txt

url = "https://aflow.org/API/aflux/?auid,aurl,compound,paging(0)"
