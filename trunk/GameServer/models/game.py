import datetime
import logging
import math
import random
import time

from django.utils import simplejson as json
from google.appengine.api import users
from google.appengine.ext import db

RADIUS_OF_EARTH_METERS = 6378100
TRIGGER_DISTANCE_METERS = 15
ZOMBIE_VISION_DISTANCE_METERS = 200
MAX_TIME_INTERVAL_SECS = 60 * 10  # 10 minutes

ZOMBIE_SPEED_VARIANCE = 0.2
MIN_NUM_ZOMBIES = 20
MIN_ZOMBIE_DISTANCE_FROM_PLAYER = 20
MAX_ZOMBIE_CLUSTER_SIZE = 4
MAX_ZOMBIE_CLUSTER_RADIUS = 30

DEFAULT_ZOMBIE_SPEED = 3 * 0.447  # x miles per hour in meters per second
DEFAULT_ZOMBIE_DENSITY = 20.0  # zombies per square kilometer

INFECTED_PLAYER_TRANSITION_SECONDS = 120

# The size of a GameTile.  A GameTile will span an area that is 0.01 degrees by
# 0.01 degrees, in both latitude and longitude.  Changing this parameter will
# invalidate all previously recorded games with undefined consequences.
#
# 360 / GAME_TILE_LAT_LON_SPAN and 180 / GAME_TILE_LAT_LON_SPAN must be integer
# values.
GAME_TILE_LAT_LON_SPAN = 0.01

class Error(Exception):
  """Base error class for all model errors."""

class ModelStateError(Error):
  """A model was in an invalid state."""

class InvalidLocationError(Error):
  """A latitude or longitude was invalid."""


class Entity():
  """An Entity is the base class of every entity in the game.
  
  Entities have a location and a last location update timestamp.
  """
  def __init__(self, game, encoded=None):
    self.game = game
    self.location = (None, None)
    if encoded:
      self.FromString(encoded)
  
  def DictForJson(self):
    return {"lat": self.Lat(), "lon": self.Lon()}
  
  def ToString(self):
    return json.dumps(self.DictForJson())
  
  def FromString(self, encoded):
    obj = json.loads(encoded)
    if obj["lat"] and obj["lon"]:
      self.SetLocation(obj["lat"], obj["lon"])
    return obj
  
  def Invalidate(self, timedelta):
    """Called to invalidate the current state, after some amount of time has
    passed.
    
    Args:
      timedelta: The amount of time that has passed since Invalidate was last
          called.  A datetime.timedelta object.
    """
  
  def Lat(self):
    return self.location[0]
  
  def Lon(self):
    return self.location[1]

  def SetLocation(self, lat, lon):
    if lat is None or lon is None:
      raise InvalidLocationError("Lat and Lon must not be None.")
    if lat > 90 or lat < -90:
      raise InvalidLocationError("Invalid latitude: %s" % lat)
    if lon > 180 or lon < -180:
      raise InvalidLocationError("Invalid longitude: %s" % lon)
    
    self.location = (lat, lon)
  
  def DistanceFrom(self, other):
    """Compute the distance to another Entity."""
    return self.DistanceFromLatLon(other.Lat(), other.Lon())
  
  def DistanceFromLatLon(self, lat, lon):
    dlon = lon - self.Lon()
    dlat = lat - self.Lat()
    a = math.sin(math.radians(dlat/2)) ** 2 + \
        math.cos(math.radians(self.Lat())) * \
        math.cos(math.radians(lat)) * \
        math.sin(math.radians(dlon / 2)) ** 2
    greatCircleDistance = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    distance = RADIUS_OF_EARTH_METERS * greatCircleDistance
    return distance


class Trigger(Entity):
  """A trigger is an element that can trigger some game action, when reached.
  
  For example: a destination is an entity in the game that triggers the 'win
    game' state.  A Zombie is an entity in the game that triggers the 'lose
    game' state.
  
  Triggers should implement the Process interface method, which gives it
  a hook to modify the game state at each elapsed interval.
  """
  
  def Trigger(self, player):
    """Process any state changes that should occur in the game when this
    trigger interacts with the specified Player."""
    # By default, no action.
    pass   
  

class Player(Trigger):
  """A player is a player of the game, obviously I hope."""

  def __init__(self, game, encoded=None, user=None):
    self.infected = False
    self.is_zombie = False
    self.reached_destination = False
    Entity.__init__(self, game, encoded)
    if user:
      self.email = user.email()
  
  def DictForJson(self):
    if self.email is None:
      raise ModelStateError("User must be set before the Player is encoded.")
    dict = Entity.DictForJson(self)
    dict["email"] = self.email
    dict["infected"] = self.infected
    if self.infected:
      dict["infected_time"] = self.infected_time
    dict["is_zombie"] = self.is_zombie
    dict["reached_destination"] = self.reached_destination
    return dict

  def FromString(self, encoded):
    obj = Entity.FromString(self, encoded)
    self.email = obj["email"]
    self.infected = obj["infected"]
    if self.infected:
      self.infected_time = obj["infected_time"]
    self.is_zombie = obj["is_zombie"]
    self.reached_destination = obj["reached_destination"]
  
  def Email(self):
    return self.email
  
  def Invalidate(self, timedelta):
    """Determines whether or not the player has transitioned from infected to
    zombie."""
    if self.infected and \
       time.time() - self.infected_time > \
           INFECTED_PLAYER_TRANSITION_SECONDS:
      self.is_zombie = True
      
  def Infect(self):
    """Call to trigger this Player getting infected by a zombie."""
    self.infected = True
    self.infected_time = time.time()
    
  def ReachedDestination(self):
    """Call to indicate that this player has reached the game's destination."""
    logging.info("Player reached destination.")
    self.reached_destination = True
  
  def HasReachedDestination(self):
    return self.reached_destination
  
  def IsInfected(self):
    return self.infected
  
  def IsZombie(self):
    return self.is_zombie
  
  def Trigger(self, player):
    if self.IsZombie():
      player.Infect()


class Zombie(Trigger):
  
  def __init__(self, game, encoded=None, speed=None, guid=None):
    if speed:
      self.speed = speed
    if guid:
      self.guid = guid

    self.chasing = None
    self.chasing_email = None
    Entity.__init__(self, game, encoded)
  
  def Id(self):
    return self.guid
  
  def Advance(self, seconds, player_iter):
    """Meander some distance.
    
    Args:
      timedelta: a datetime.timedelta object indicating how much time has
          elapsed since the last time we've advanced the game.
      player_iter: An iterator that will walk over the players in the game.
    """
    # Advance in 1-second increments
    players = [player for player in player_iter]
    while seconds > 0:
      distance_to_move = seconds * self.speed
      self.ComputeChasing(players)
      if self.chasing:
        distance = self.DistanceFrom(self.chasing)
        self.MoveTowardsLatLon(self.chasing.Lat(),
                               self.chasing.Lon(),
                               min(distance, distance_to_move))
      else:
        random_lat = self.Lat() + random.random() - 0.5
        random_lon = self.Lon() + random.random() - 0.5
        self.MoveTowardsLatLon(random_lat, random_lon, distance_to_move)
      seconds = seconds - 1
      
  def MoveTowardsLatLon(self, lat, lon, distance):
    dstToLatLon = self.DistanceFromLatLon(lat, lon)
    magnitude = 0
    if dstToLatLon > 0:
      magnitude = distance / dstToLatLon
    dLat = (lat - self.Lat()) * magnitude
    dLon = (lon - self.Lon()) * magnitude
    self.SetLocation(self.Lat() + dLat, self.Lon() + dLon)
  
  def ComputeChasing(self, player_iter):
    min_distance = None
    min_player = None
    for player in player_iter:
      distance = self.DistanceFrom(player)
      if min_distance is None or distance < min_distance:
        min_distance = distance
        min_player = player
    
    if min_distance and min_distance < ZOMBIE_VISION_DISTANCE_METERS:
      self.chasing = min_player
      self.chasing_email = min_player.Email()
    else:
      self.chasing = None
      self.chasing_email = None

  def Trigger(self, player):
    player.Infect()
  
  def DictForJson(self):
    dict = Entity.DictForJson(self)
    dict["speed"] = self.speed
    dict["guid"] = self.guid
    if self.chasing_email:
      dict["chasing"] = self.chasing_email
    return dict
  
  def FromString(self, encoded):
    obj = Entity.FromString(self, encoded)
    self.speed = float(obj["speed"])
    self.guid = int(obj["guid"])
    if obj.has_key("chasing"):
      self.chasing_email = obj["chasing"]


class Destination(Trigger):
  
  def Trigger(self, player):
    player.ReachedDestination()


class LatLngBounds(Object):
  
  def __init__(self, neLat, neLon, swLat, swLon):
    self.neLat = neLat
    self.neLon = neLon
    self.swLat = swLat
    self.swLon = swLon
    
  def Contains(self, lat, lon):
    return lat < self.neLat and \
        lat > self.swLat and \
        lon > self.neLon and \
        lon < self.swLon
    

def ZombieEquals(a, b):
  return a.Id() == b.Id()


class GameTile(db.Model):
  """A GameTile represents a small geographical section of a ZombieRun game.

  A GameTile always has a Game as its parent, so one can always retrieve the
  game that a GameTile belongs to by calling game_tile.parent().
  
  There is a lot of copy-paste here, the only thing changing generally is the
  accessor to the id of the Zombie or of the Player.  That should be refactored.
  """

  # The list of player emails, for querying.
  player_emails = db.StringListProperty()
  
  # The actual encoded player data.
  players = db.StringListProperty()

  zombies = db.StringListProperty()

  last_update_time = db.DateTimeProperty(auto_now=True)
  
  def __init__(self):
    self.decoded_players = None
    self.decoded_zombies = None
  
  def Players(self):
    if self.decoded_players is not None:
      return self.decoded_players
    
    self.decoded_players = [Player(self, e) for e in self.players]
    return self.decoded_players
  
  def AddPlayer(self, player):
    assert not self.HasPlayer(player)
    self.players.append(player.ToString())
    self.player_emails.append(player.Email())
    self._InvalidateDecodedPlayers()
  
  def HasPlayer(self, player):
    # TODO: optimize.
    for p in self.Players():
      if p.Email() == player.Email():
        return True
    return False
  
  def SetPlayer(self, player):
    # TODO: This can be optimized to use a "GetOrNone" operation.
    assert self.HasPlayer(player)
    # TODO: optimize this with a hash map at construction
    for i, p in enumerate(self.Players()):
      if p.Email() == player.Email():
        self.players[index] = player.ToString()
        self.player_emails[index] = player.Email()
    self._InvalidateDecodedPlayers()
    
  def RemovePlayer(self, player):
    assert self.HasPlayer(player)
    player_removed = False
    for i, p in enumerate(self.Players()):
      if p.Email() == player.Email():
        self.players.pop(i)
        player_removed = True
        break
    assert player_removed
    self._InvalidateDecodedPlayers()

  def _InvalidateDecodedPlayers(self):
    self.decoded_players = None
  
  def Zombies(self):
    if self.decoded_zombies is not None:
      return self.decoded_zombies
    
    self.decoded_zombies = [Zombie(self, e) for e in self.zombies]
    return self.decoded_zombies
  
  def AddZombie(self, zombie):
    assert not self.HasZombie(zombie)
    self.zombies.append(zombie.ToString())
    self._InvalidateDecodedZombies()
  
  def HasZombie(self, zombie):
    for z in self.Zombies():
      if z.Id() == zombie.Id():
        return True
    return False
  
  def RemoveZombie(self, zombie):
    zombie_removed = False
    for i, z in enumerate(self.Zombies()):
      if z.Id() == zombie.Id():
        self.zombies.pop(i)
        zombie_removed = True
    assert zombie_removed
    self._InvalidateDecodedZombies()
  
  def SetZombie(self, zombie):
    self._SetEntity(zombie, self.zombies, self.Zombies, ZombieEquals)
    self._InvalidateDecodedZombies()
  
  def _InvalidateDecodedZombies(self):
    self.decoded_zombies = None
  
  def _SetEntity(self, entity, entities, Entities, Equals):
    set = False
    for i, e in enumerate(Entities()):
      if Equals(e, entity):
        entities[i] = e.ToString()
        set = True
        break
    assert set


class GameTileWindow(Object):
  """A GameTileWindow is a utility class for dealing with a set of GameTiles."""

  def __init__(self, game, lat, lon, radius_meters):
    self.game = game
    # Retrieve the game tiles that intersect the circle described by the lat,
    # lon, and radius.
    #
    # Create and populate them with zombies if they don't exist.
    self.tiles = {}
  
  def Players(self):
    for tile in self.tiles:
      for player in tile.Players():
        yield player

  def AddPlayer(self, player):
    # find tile
    self._TileForEntity(player).AddPlayer(player)
  
  def SetPlayer(self, player):
    self._TileForEntity(player).SetPlayer(player)

  def Zombies(self, zombie):
    for tile in self.tiles.itervalues():
      for zombie in tile.Zombies():
        yield zombie
  
  def AddZombie(self, zombie):
    self._TileForEntity(zombie).AddZombie(zombie)
  
  def SetZombie(self, zombie):
    # First find the zombie in the tile it exists right now, and determine
    # whether or not the zombie is moving from one tile to another.
    original_tile = None
    for tile in self.tiles.itervalues():
      if tile.HasZombie(zombie):
        original_tile = tile
    
    new_tile = self._TileForEntity(zombie)
    
    self._TileForEntity(zombie).SetZombie(zombie)
    
  def _TileForEntity(self, entity):
    # We assume in these calculations that 360 / GAME_TILE_LAT_LON_SPAN and
    # 180 / GAME_TILE_LAT_LON_SPAN both come out to an integer value.
    
    # identify the column of GameTiles at longitude -180 to be column 0.
    # we have a total of 360 / GAME_TILE_LAT_LON_SPAN columns.
    # 
    # So, the column that this entity lies in is:
    #    portion_into_columns * num_columns =
    #    ((lon + 180) / 360) * (360 / GAME_TILE_LAT_LON_SPAN) =
    #    (lon + 180) / GAME_TILE_LAT_LON_SPAN
    #
    # Which is then rounded down to an integer id.
    column = int((entity.Lon() + 180) / GAME_TILE_LAT_LON_SPAN)
    
    # Similar logic for the row
    row = int((entity.Lat() + 90) / GAME_TILE_LAT_LON_SPAN)

    # ID of the game tile is defined as:
    #
    # column * NUM_ROWS_PER_COLUMN + row
    id = (column * 180 / GAME_TILE_LAT_LON_SPAN) + row
    
    
    
    return None
  
  def _GetGameTile(self, tile_id):
    if self.tiles.has_key(tile_id):
      return self.tiles[tile_id]
    
    if (self._GetGameTileFromMemcache(tile_id) or
        self._GetGameTileFromDatastore(tile_id)):
      return self._GetGameTile(tile_id)
    else:
      # TODO: create and put a game tile.
      pass
    
  def _GetGameTileFromMemcache(self, tile_id):
    encoded = memcache.get(self._GetGameTileKeyName(tile_id))
    
    if not encoded:
      logging.warn("Memcache game tile miss.")
      return False
    
    try:
      tile = pickle.loads(encoded)
      self.tiles[tile_id] = tile
      return True
    except pickle.UnpicklingError, e:
      logging.error("UnpicklingError on GameTile: %s" % e)
      return False
  
  def _GetGameTileFromDatastore(self, tile_id):
    logging.info("Getting game tile from datastore.")
    tile = GameTile.get_by_key_name(self._GetGameTileKeyName(tile_id),
                                    self.game)
    if tile:
      self.tiles[tile_id] = tile
      return True
    else:
      return False
  
  def _GetGameTileKeyName(self, tile_id):
    return "gt%d" % tile_id


class Game(db.Model):
  """A Game contains all the information about a ZombieRun game."""
  
  owner = db.UserProperty(auto_current_user_add=True)
  
  destination = db.StringProperty()
  
  # Meters per Second
  average_zombie_speed = db.FloatProperty(default=DEFAULT_ZOMBIE_SPEED)
  
  # Zombies / km^2
  zombie_density = db.FloatProperty(default=DEFAULT_ZOMBIE_DENSITY)
  
  game_creation_time = db.DateTimeProperty(auto_now_add=True)
  last_update_time = db.DateTimeProperty(auto_now=True)
  
  def _GameTileWindow(self):
    pass
  
  def Id(self):
    # Drop the "g" at the beginning of the game key name.
    return int(self.key().name()[1:])
  
  def Players(self):
    for player in self._GameTileWindow().Players():
      yield player
  
  def ZombiePlayers(self):
    for player in self.Players():
      if player.IsZombie():
        yield player
  
  def PlayersInPlay(self):
    """Iterate over the Players in the Game which have locations set, have not
    reached the destination, and are not infected.
    
    Returns:
        Iterable of (player_index, player) tuples.
    """
    for i, player in enumerate(self.Players()):
      if (player.Lat() and 
          player.Lon() and 
          not player.HasReachedDestination() and
          not player.IsInfected()):
        yield i, player
  
  def AddPlayer(self, player):
    self._GameTileWindow().AddPlayer(player)
  
  def SetPlayer(self, player):
    self._GameTileWindow().SetPlayer(player)
  
  def Zombies(self):
    for zombie in self._GameTileWindow().Zombies():
      yield zombie
  
  def ZombiesAndInfectedPlayers(self):
    entities = []
    entities.extend(self.Zombies())
    entities.extend(self.ZombiePlayers())
    return entities
  
  def AddZombie(self, zombie):
    self._GameTileWindow().AddZombie(zombie)
    
  def SetZombie(self, zombie):
    self._GameTileWindow().SetZombie(zombie)
  
  def Destination(self):
    return Destination(self, self.destination)
  
  def SetDestination(self, destination):
    self.destination = destination.ToString()
  
  def Entities(self):
    """Iterate over all Entities in the game."""
    for zombie in self.Zombies():
      yield zombie
    for player in self.Players():
      yield player
    yield self.Destination()
  
  def Advance(self):
    timedelta = datetime.datetime.now() - self.last_update_time
    seconds = timedelta.seconds + timedelta.microseconds / float(1e6)
    seconds_to_move = min(seconds, MAX_TIME_INTERVAL_SECS)
    
    for entity in self.Entities():
      entity.Invalidate(timedelta)

    players_in_play = [player for i, player in self.PlayersInPlay()]
    for zombie in enumerate(self.Zombies()):
      zombie.Advance(seconds_to_move, players_in_play)
      self.SetZombie(zombie)
      
    # Perform triggers.
    for i, player in self.PlayersInPlay():
      # Trigger destination first, so that when a player has reached the
      # destination at the same time they were caught by a zombie, we give them
      # the benefit of the doubt.
      destination = self.Destination()
      if player.DistanceFrom(destination) < TRIGGER_DISTANCE_METERS:
        destination.Trigger(player)

      for zombie in self.ZombiesAndInfectedPlayers():
        if player.DistanceFrom(zombie) < TRIGGER_DISTANCE_METERS:
          zombie.Trigger(player)
      self.SetPlayer(i, player)
