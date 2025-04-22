from flask import Flask, render_template, request, redirect, url_for, session
from docx import Document
import pandas as pd
import io
import re
from sqlalchemy import create_engine
import math
import os
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'your-secret-key-here')

def extract_table_alternative(docx_bytes):
    try:
        document = Document(io.BytesIO(docx_bytes))
        tables = document.tables
        if not tables:
            return None
        table = tables[1]
        header_row = table.rows[0]
        data_rows = [row for row in table.rows]
        if not data_rows or len(data_rows) < 2:
            return pd.DataFrame()
        num_cols = len(data_rows[1].cells)
        columns = [cell.text.strip() for cell in header_row.cells[:num_cols]]
        if not columns:
            columns = [f"Column_{i+1}" for i in range(num_cols)]
        table_data = []
        for row in data_rows[1:]:
            row_values = [cell.text for cell in row.cells]
            if len(row_values) < num_cols:
                row_values.extend([''] * (num_cols - len(row_values)))
            elif len(row_values) > num_cols:
                row_values = row_values[:num_cols]
            table_data.append(row_values)
        df = pd.DataFrame(table_data, columns=columns)
        return df
    except Exception as e:
        print(f"Error extracting table (alternative method): {e}")
        return None

def convert_date_format(date_str):
    try:
        pd_date = pd.to_datetime(date_str, format='%m/%d/%Y', errors='raise')
        return pd_date.strftime('%Y-%m-%d')
    except ValueError:
        try:
            pd_date = pd.to_datetime(date_str, format='%d.%m.%Y', errors='raise')
            return pd_date.strftime('%Y-%m-%d')
        except ValueError:
            return date_str
        
import unicodedata

def clean_text(text):
    if not isinstance(text, str):
        return text
    return unicodedata.normalize('NFKC', text.strip())

def process_data(docx_file_bytes, excluded_survey_numbers_str):
    try:
        df = extract_table_alternative(docx_file_bytes)
        if df is None or df.empty:
            return "No table found or error during extraction.", "", pd.DataFrame()
        df.columns = df.iloc[0]
        df1 = df[1:]
        if 'दिनांक' in df1.columns:
            df1['दिनांक'] = df1['दिनांक'].apply(convert_date_format)
        df1['दस्ताचा प्रकार'] = df1['दस्ताचा प्रकार'].apply(clean_text)
        engine = create_engine('sqlite:///:memory:')
        table_name = 'mytable'
        df1.to_sql(table_name, engine, if_exists='replace', index=False)
        date_column = 'दिनांक'
        price_column = 'खरेदी किंमत'
        p_column = 'प्रती चौ.मी.'
        deed_type_column = 'दस्ताचा प्रकार'
        start_date = '2020-05-02'
        end_date = '2023-05-02'
        deed_values = ['कन्व्हेन्स डीड', 'कन्व्हेअन्स डीड', 'कन्‍व्हेन्स डीड', '65-चुक दुरुस्ती पत्र', '65 चुक दुरुस्ती पत्र', 'करारनामा']
        first_filter_query = f"SELECT * FROM {table_name} WHERE `{date_column}` BETWEEN '{start_date}' AND '{end_date}'"
        second_filter_query = f"SELECT * FROM ({first_filter_query}) WHERE `{price_column}` NOT IN (0, 1) AND `{p_column}` NOT BETWEEN 0 AND 10"
        deed_values_str = ', '.join([f"'{v}'" for v in deed_values])
        third_filter_query = f"SELECT * FROM ({second_filter_query}) WHERE `{deed_type_column}` NOT IN ({deed_values_str})"
        final_table = pd.read_sql(third_filter_query, engine)
        final_table_name = 'final_filtered_table'
        final_table.to_sql(final_table_name, engine, if_exists='replace', index=False)
        survey_no_column = 'सर्व्हे नं.'
        excluded_numbers = [num.strip() for num in excluded_survey_numbers_str.replace(' ', ',').split(',') if num.strip().isdigit()]
        excluded_condition = ""
        if excluded_numbers:
            excluded_numbers_str = ', '.join(map(str, excluded_numbers))
            excluded_condition = f"AND CAST(SUBSTR(`{survey_no_column}`, 1, INSTR(`{survey_no_column}`, '/') - 1) AS INTEGER) NOT IN ({excluded_numbers_str})"
        survey_no_filter_query = f"SELECT * FROM {final_table_name} WHERE INSTR(`{survey_no_column}`, '/') = 0 {excluded_condition} OR (INSTR(`{survey_no_column}`, '/') > 0 {excluded_condition})"
        df_filtered_survey = pd.read_sql(survey_no_filter_query, engine)
        df_filtered_survey = df_filtered_survey[~((df_filtered_survey['दस्ताचा प्रकार'] == 'अभिहस्तांतरणपत्र') & (df_filtered_survey['खरेदी किंमत'] != '600000'))]
        df_filtered_survey[p_column] = pd.to_numeric(df_filtered_survey[p_column], errors='coerce')
        sorted_df = df_filtered_survey.sort_values(by=p_column, ascending=False)
        half_rows = math.floor(len(sorted_df) / 2) if len(sorted_df) > 0 else 0
        top_half_df = sorted_df.head(half_rows)
        average_sqm = top_half_df[p_column].mean() if p_column in top_half_df.columns and not top_half_df.empty else None
        return f"The average of these {half_rows} purchase and sale transactions is Rs. {average_sqm:.2f}/- per sq. m." if average_sqm else "Could not calculate the average 'Per sq. M.' due to missing data or column.", f"सदर {half_rows} खरेदी विक्री व्यवहारांची सरासरी रु. {average_sqm:.2f}/- प्रती चौ. मी." if average_sqm else "", top_half_df
    except Exception as e:
        return f"An error occurred: {e}", "", pd.DataFrame()

@app.route('/')
def login():
    return render_template('login.html')

@app.route('/login', methods=['POST'])
def login_post():
    user_id = request.form['user_id']
    password = request.form['password']
    if user_id == 'admin' and password == '5555':
        session['logged_in'] = True
        return redirect(url_for('index'))
    return render_template('login.html', error='Invalid credentials')

@app.route('/index')
def index():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    return render_template('index.html')

@app.route('/process', methods=['POST'])
def process():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    docx_file = request.files['input_file']
    excluded_survey_numbers = request.form['excluded_survey_numbers']
    result_en, result_mr, table = process_data(docx_file.read(), excluded_survey_numbers)
    return render_template('index.html', result_en=result_en, result_mr=result_mr, table=table.to_html(classes='data', header=True))

if __name__ == '__main__':
    app.run(debug=True, port=5001)
