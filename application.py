import re
import os
import datetime
from flask import Flask, abort
import humanfriendly
from flask_sqlalchemy import SQLAlchemy
from flask_restful import Resource, Api, reqparse
from raven.contrib.flask import Sentry
import sqlalchemy


app = Flask(__name__)
sentry = Sentry(app)
application = app  # noqa
app.config['DEBUG'] = os.environ.get('APP_DEBUG', False)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

if 'RDS_HOSTNAME' in os.environ:
    db_creds = {'user': os.environ['RDS_USERNAME'],
                'password': os.environ['RDS_PASSWORD'],
                'host': os.environ['RDS_HOSTNAME'],
                'port': os.environ['RDS_PORT'],
                'database': os.environ['RDS_DB_NAME']}
    app.config[
        'SQLALCHEMY_DATABASE_URI'] = 'mysql://{user}:{password}@{host}:{port}/{database}'.format(**db_creds)
else:
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + \
        os.path.expanduser('~/{}.db'.format('slack-locate'))

app.config['SLACK_TOKEN'] = os.environ.get('SLACK_TOKEN', '')
db = SQLAlchemy(app)
api = Api(app)


class MalformedRequest(Exception):
    pass


def parse_date(date):
    date = date.strip().lower()
    # TODO: handle day names (e.g. 'monday', 'tuesday', ...)
    if date == 'today':
        return datetime.date.today()
    elif date == 'tomorrow':
        return datetime.date.today() + datetime.timedelta(days=1)
    else:
        return datetime.date(*humanfriendly.parse_date(date)[:3])


def parse_command(text):
    mode = text.split()[0]
    if mode == 'set':
        text = ' '.join(text.split()[1:])
        time_regexp = '(?P<place>\S+)( (?P<start>\S+)?( (to|until)? (?P<end>\S+)?){0,1}){0,1}'
        details = re.match(time_regexp, text)
        if details:
            details = details.groupdict()
            for moment in ('start', 'end'):
                if details[moment]:
                    details[moment] = parse_date(details[moment])
                details['place'] = details['place'].lower()
            details['action'] = 'set'
            return details
        else:
            raise MalformedRequest(text)
    else:
        username = text.strip().lstrip('@').lower()
        return {'action': 'get', 'name': username}


class Team(db.Model):
    id = db.Column(db.Unicode(64), primary_key=True)
    domain = db.Column(db.Unicode(256))


class User(db.Model):
    id = db.Column(db.Unicode(64), primary_key=True)
    name = db.Column(db.Unicode(256))
    team_id = db.Column(db.Unicode(64), db.ForeignKey('team.id'))
    team = db.relationship('Team', backref=db.backref('users'))

    def __repr__(self):
        return '<User {} "{}">'.format(self.id, self.name)

    def location(self):
        return self.locations[-1].place


class Location(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    start_date = db.Column(db.DateTime)
    end_date = db.Column(db.DateTime)
    place = db.Column(db.Unicode(256))
    user_id = db.Column(db.Unicode(64), db.ForeignKey('user.id'))
    user = db.relationship('User', backref=db.backref('locations'))

    def __repr__(self):
        return '<Location {}@{}>'.format(self.user.name, self.place)


class LocationService(Resource):

    def post(self):
        parser = reqparse.RequestParser(bundle_errors=True)
        parser.add_argument('token', type=str, required=True)
        parser.add_argument('user_id', type=str, required=True)
        parser.add_argument('user_name', type=str, required=True)
        parser.add_argument('team_id', type=str, required=True)
        parser.add_argument('team_domain', type=str, required=True)
        parser.add_argument('text', type=str, required=True)

        args = parser.parse_args()

        if args['token'] != app.config['SLACK_TOKEN']:
            abort(403, 'wrong Slack token')

        s = db.session
        command = parse_command(args['text'])

        if command['action'] == 'set':
            user = User.query.get(args['user_id'])
            if not user:
                team = Team.query.get(args['team_id'])
                if not team:
                    team = Team(id=args['team_id'], domain=args['team_domain'])
                    s.add(team)
                user = User(id=args['user_id'],
                            name=args['user_name'].lower(),
                            team=team)
                s.add(user)
            location = Location(start_date=command['start'],
                                end_date=command['end'],
                                user=user,
                                place=command['place'])
            s.add(location)
            s.commit()
            return {'text': 'Your position is now: {}'.format(location.place)}
        elif command['action'] == 'get':
            try:
                target = User.query.filter_by(name=command['name'],
                                              team_id=args['team_id']).one()
            except sqlalchemy.orm.exc.NoResultFound:
                return {'text': 'Sorry, no known location for {}'.format(command['name'])}
            place = target.location()
            return {'text': "{} is in/at {} today".format(target.name,
                                                          place.title())}
        abort(400)

api.add_resource(LocationService, '/')

if not app.config['TESTING']:
    db.create_all()

if __name__ == '__main__':
    app.run()
